"""Key-API op telemetry (issue #26, deliverable b) — pure state + Prometheus text, no I/O.

The daily key-op budget (`keys-modify-api-rpd-*`) is a capacity dimension the operator spends on
every reconcile — mint, budget patch, delete — and nothing measured it until it ran out. This
module counts those ops on the SAME window the limit resets on (the UTC calendar day), so a
threshold alert can warn before exhaustion instead of after.

`MeteredPort` is the collection point: it wraps any `OpenRouterPort`, so the counter can only ever
drift from reality if a call bypasses the port entirely. The HTTP exporter that serves `render()`
is I/O glue and lives in `operator.py` with the rest of the untested boundary; the *state* stays
here, pure and clock-injected, so it is decision-table tested offline like everything else.

Metric surface (the chart's Service / ServiceMonitor / PrometheusRule land in #27):

    openrouter_key_api_ops_total{op=...}                  counter, since process start
    openrouter_key_api_ops_today{op=...}                  gauge, current UTC day
    openrouter_key_api_rate_limited_total{limit_class=...} counter, daily-class vs other 429s

Issue #29 adds the account-scope half of the same incident — the balance the keys spend out of
(`GET /api/v1/credits`), polled on a slow timer rather than per reconcile:

    openrouter_account_credit_usd                         gauge, total_credits - total_usage
    openrouter_account_credit_updated_timestamp_seconds   gauge, last SUCCESSFUL poll (0 = never)
    openrouter_account_credit_poll_failures_total         counter, polls that failed
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from .models import ResetInterval
from .ports import AccountCredits, AccountPort, KeyState, MintedKey, OpenRouterPort, RateLimited
from .reconcile import ParkUntilReset, decide_retry

# Every port operation gets a series, including the read: `get` does not spend the keys-modify
# budget, but seeing it next to the modify ops is what tells an operator whether a spike is
# reconcile churn or genuine key turnover. #27's alert sums the modify ops only.
KEY_OPS: tuple[str, ...] = ("get", "create", "update", "delete")

# 429 classes, mirroring decide_retry: `daily` is the parked-until-reset kind (the incident),
# `other` backs off normally. Both are rendered always so a rate() has a series from t0.
LIMIT_CLASSES: tuple[str, ...] = ("daily", "other")

# Bounds on the credits poll (issue #29). It is an account-scope number, so 5 minutes is plenty;
# the floor exists so a mistyped chart value cannot turn a gauge into a key-API hammer.
DEFAULT_CREDIT_POLL_INTERVAL_S = 300.0
MIN_CREDIT_POLL_INTERVAL_S = 60.0


class KeyOpMetrics:
    """Counts key-API ops per UTC day (the rpd window) and per process lifetime.

    Also OWNS the account-credit state (`.account`) and renders it. That composition is
    deliberate rather than tidy: `:9090/metrics` is one exposition surface, and
    `test_chart_metrics.py::test_every_alert_has_a_series_behind_it` refuses any chart alert whose
    metric is missing from `KeyOpMetrics().render()` — so a series that lives on some other
    renderer is, to that guard, a series that does not exist.
    """

    def __init__(self) -> None:
        self.account = AccountCreditMetrics()
        self._total: dict[str, int] = dict.fromkeys(KEY_OPS, 0)
        self._today: dict[str, int] = dict.fromkeys(KEY_OPS, 0)
        self._rate_limited: dict[str, int] = dict.fromkeys(LIMIT_CLASSES, 0)
        self._day: date | None = None
        self._missing_secrets: set[str] = set()

    def record(self, op: str, now: datetime) -> None:
        """Count one key-API request — issued, not necessarily successful: a 429'd request still
        spent budget, and counting only successes would under-read the exhaustion it signals."""
        self._roll(now)
        self._total[op] += 1
        self._today[op] += 1

    def record_rate_limited(self, limit_name: str | None, now: datetime) -> None:
        """Count a 429, classed by `decide_retry` — the same verdict the handler acted on, so the
        metric cannot claim `daily` for an op the operator merely backed off on."""
        self._roll(now)
        parked = isinstance(decide_retry(limit_name, now), ParkUntilReset)
        self._rate_limited["daily" if parked else "other"] += 1

    def record_secret_missing(self, secret_name: str) -> None:
        """Track that a Secret is missing — gauge counts currently-missing Secrets."""
        self._missing_secrets.add(secret_name)

    def record_secret_found(self, secret_name: str) -> None:
        """Track that a Secret is no longer missing — gauge counts currently-missing Secrets."""
        self._missing_secrets.discard(secret_name)

    def ops_today(self, now: datetime) -> int:
        self._roll(now)
        return sum(self._today.values())

    def ops_total(self) -> int:
        return sum(self._total.values())

    def render(self, now: datetime) -> str:
        """Prometheus text exposition. Label values are fixed literals from the tuples above, so
        no escaping is needed — nothing user-supplied reaches a label."""
        self._roll(now)
        lines = [
            "# HELP openrouter_key_api_ops_total Key-API operations issued since operator start.",
            "# TYPE openrouter_key_api_ops_total counter",
            *(f'openrouter_key_api_ops_total{{op="{op}"}} {self._total[op]}' for op in KEY_OPS),
            "# HELP openrouter_key_api_ops_today Key-API operations issued in the current UTC "
            "day, the window the keys-modify rpd budget resets on.",
            "# TYPE openrouter_key_api_ops_today gauge",
            *(f'openrouter_key_api_ops_today{{op="{op}"}} {self._today[op]}' for op in KEY_OPS),
            "# HELP openrouter_key_api_rate_limited_total Key-API 429s, by whether the limit is "
            "daily (parked until the UTC reset) or not.",
            "# TYPE openrouter_key_api_rate_limited_total counter",
            *(
                f'openrouter_key_api_rate_limited_total{{limit_class="{cls}"}} '
                f"{self._rate_limited[cls]}"
                for cls in LIMIT_CLASSES
            ),
        ]
        lines += [
            "# HELP openrouter_secret_missing Number of k8s Secrets for healthy upstream keys that "
            "are absent (deleted out of band); 0 when all Secrets are present (issue #56).",
            "# TYPE openrouter_secret_missing gauge",
            f"openrouter_secret_missing {len(self._missing_secrets)}",
        ]
        return "\n".join(lines) + "\n" + self.account.render()

    def _roll(self, now: datetime) -> None:
        """Reset the daily gauge when the UTC day turns. Called on READ as well as on write: a
        quiet operator is still scraped, and a gauge left at yesterday's total would hold the
        threshold alert firing straight through the reset that already fixed it."""
        day = now.astimezone(UTC).date()
        if day != self._day:
            self._day = day
            self._today = dict.fromkeys(KEY_OPS, 0)


class AccountCreditMetrics:
    """Last known OpenRouter account balance, and how much that reading can be trusted (#29).

    Two failure modes shape this, and both are about NOT emitting a plausible lie:

    * A failed poll must never render as `0`. An unreachable API and a drained account would then
      look identical, and #33's low-water alert would page on a network blip while a real drain
      hides behind the same number. So a failure holds the last good reading and moves only the
      freshness signals — the balance changes when, and only when, OpenRouter says it did.
    * Before the FIRST successful poll there is no last good reading at all. The sample is `NaN`,
      the exposition format's explicit no-value: the series is present (which the chart guard
      requires — see `KeyOpMetrics`), every comparison against it is false, so a threshold alert
      can neither fire on it nor read it as $0.

    Staleness is therefore what an operator alerts on for "the poller is broken", via
    `time() - openrouter_account_credit_updated_timestamp_seconds`. That timestamp is 0 (not NaN)
    when nothing has ever succeeded, so a never-polled operator reads as maximally stale and
    trips that rule loudly, rather than disappearing from it.
    """

    def __init__(self) -> None:
        self._balance_usd: float = math.nan
        self._updated_at: datetime | None = None
        self._poll_failures = 0

    def record_credits(self, credits: AccountCredits, now: datetime) -> None:
        self._balance_usd = credits.balance_usd
        self._updated_at = now

    def record_poll_failure(self) -> None:
        """Count a failed poll. Takes no clock on purpose: nothing about a failure is allowed to
        touch the balance or its freshness — a stale reading must keep looking exactly as stale
        as it is."""
        self._poll_failures += 1

    def render(self) -> str:
        """Prometheus text exposition. No labels and no time-dependent state — unlike the daily
        op gauge, nothing here rolls over on a clock."""
        updated = self._updated_at.timestamp() if self._updated_at is not None else 0.0
        lines = [
            "# HELP openrouter_account_credit_usd OpenRouter account credit remaining "
            "(total_credits - total_usage) as of the last successful poll; NaN before the first.",
            "# TYPE openrouter_account_credit_usd gauge",
            f"openrouter_account_credit_usd {_fmt(self._balance_usd)}",
            "# HELP openrouter_account_credit_updated_timestamp_seconds Unix time of the last "
            "SUCCESSFUL credits poll; 0 when none has ever succeeded.",
            "# TYPE openrouter_account_credit_updated_timestamp_seconds gauge",
            f"openrouter_account_credit_updated_timestamp_seconds {_fmt(updated)}",
            "# HELP openrouter_account_credit_poll_failures_total Credits polls that failed; the "
            "balance above holds its last known value across these rather than reading 0.",
            "# TYPE openrouter_account_credit_poll_failures_total counter",
            f"openrouter_account_credit_poll_failures_total {self._poll_failures}",
        ]
        return "\n".join(lines) + "\n"


def _fmt(value: float) -> str:
    """Render a float for the exposition format: fixed-point, trailing zeros trimmed, and `NaN`
    spelled the way Prometheus' parser expects. Six decimals is far below a cent."""
    if math.isnan(value):
        return "NaN"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def poll_account_credit(
    port: AccountPort, metrics: AccountCreditMetrics, clock: Callable[[], datetime]
) -> None:
    """One balance refresh: read the ledger through the port, record success or failure.

    The broad `except` is the point of this function. It runs on a background timer thread, so
    every failure — network, 429, a credential the endpoint rejects, an SDK shape change — has to
    degrade to "the balance is stale" instead of killing the poller or zeroing the gauge. There is
    no caller to re-raise to and nothing to decide: the only distinction that reaches a metric is
    succeeded vs did not.
    """
    try:
        credits = port.get_account_credits()
    except Exception:
        metrics.record_poll_failure()
        return
    metrics.record_credits(credits, clock())


def credit_poll_interval_s(raw: str | None) -> float:
    """Bound the configured poll interval (`METRICS_CREDIT_INTERVAL`).

    Pure and tested because it runs at operator startup: an unparseable value must fall back to
    the default, never take the process down with a ValueError before kopf has a single handler
    running.
    """
    try:
        interval = float(raw) if raw is not None and raw.strip() else DEFAULT_CREDIT_POLL_INTERVAL_S
    except ValueError:
        return DEFAULT_CREDIT_POLL_INTERVAL_S
    return max(MIN_CREDIT_POLL_INTERVAL_S, interval)


class MeteredPort:
    """An `OpenRouterPort` that counts every call it forwards (and every 429 that comes back).

    Wrapping the port rather than instrumenting the handlers means a new call site is counted by
    construction — the untracked-dimension failure this issue is about cannot recur by omission.
    """

    def __init__(
        self, inner: OpenRouterPort, metrics: KeyOpMetrics, clock: Callable[[], datetime]
    ) -> None:
        self._inner = inner
        self._metrics = metrics
        self._clock = clock

    def get_key(self, key_hash: str) -> KeyState | None:
        with self._counted("get"):
            return self._inner.get_key(key_hash)

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey:
        with self._counted("create"):
            return self._inner.create_key(name, limit, reset, expires_at)

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None:
        with self._counted("update"):
            self._inner.update_key(key_hash, limit, reset)

    def delete_key(self, key_hash: str) -> None:
        with self._counted("delete"):
            self._inner.delete_key(key_hash)

    @contextmanager
    def _counted(self, op: str) -> Iterator[None]:
        self._metrics.record(op, self._clock())
        try:
            yield
        except RateLimited as exc:
            self._metrics.record_rate_limited(exc.limit_name, self._clock())
            raise
