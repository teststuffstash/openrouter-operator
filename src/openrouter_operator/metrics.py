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
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from .models import ResetInterval
from .ports import KeyState, MintedKey, OpenRouterPort, RateLimited
from .reconcile import ParkUntilReset, decide_retry

# Every port operation gets a series, including the read: `get` does not spend the keys-modify
# budget, but seeing it next to the modify ops is what tells an operator whether a spike is
# reconcile churn or genuine key turnover. #27's alert sums the modify ops only.
KEY_OPS: tuple[str, ...] = ("get", "create", "update", "delete")

# 429 classes, mirroring decide_retry: `daily` is the parked-until-reset kind (the incident),
# `other` backs off normally. Both are rendered always so a rate() has a series from t0.
LIMIT_CLASSES: tuple[str, ...] = ("daily", "other")


class KeyOpMetrics:
    """Counts key-API ops per UTC day (the rpd window) and per process lifetime."""

    def __init__(self) -> None:
        self._total: dict[str, int] = dict.fromkeys(KEY_OPS, 0)
        self._today: dict[str, int] = dict.fromkeys(KEY_OPS, 0)
        self._rate_limited: dict[str, int] = dict.fromkeys(LIMIT_CLASSES, 0)
        self._day: date | None = None

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
        return "\n".join(lines) + "\n"

    def _roll(self, now: datetime) -> None:
        """Reset the daily gauge when the UTC day turns. Called on READ as well as on write: a
        quiet operator is still scraped, and a gauge left at yesterday's total would hold the
        threshold alert firing straight through the reset that already fixed it."""
        day = now.astimezone(UTC).date()
        if day != self._day:
            self._day = day
            self._today = dict.fromkeys(KEY_OPS, 0)


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
