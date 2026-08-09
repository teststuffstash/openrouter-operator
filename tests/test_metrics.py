"""Decision-table tests for the key-API op telemetry (issue #26, deliverable b).

The daily key-op budget was an untracked capacity dimension: today's record ride volume × (mint +
budget patch + delete) exhausted a limit nobody measured, and the crosscheck read "healthy" because
no alert exists for it. `KeyOpMetrics` counts what the operator actually spends against that
budget. The table pins the part that is easy to get wrong: the counting window is the UTC calendar
day — the same window the rpd limit resets on — so a scrape must never report yesterday's ops as
today's, and the rollover has to happen on READ as well as on write (a quiet operator still scrapes
every 30s, and a gauge stuck at yesterday's total would fire the threshold alert forever).

The account-balance gauge (issue #29) is the second half of the same incident — the fleet stalled on
the daily key-op limit AND on a $0.17 pay-as-you-go balance, and nothing measured either. Its table
pins a different failure: the balance is polled on a slow timer, so the poll can fail, and it can
fail before it has ever succeeded. A failed poll must never be indistinguishable from a drained
account — that difference is precisely what the low-water alert (#33) fires on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from openrouter_operator.metrics import (
    KEY_OPS,
    KeyOpMetrics,
    MeteredPort,
    credit_poll_interval_s,
    poll_account_credit,
)
from openrouter_operator.models import ResetInterval
from openrouter_operator.ports import (
    AccountCredits,
    AccountPort,
    KeyState,
    MintedKey,
    OpenRouterPort,
    RateLimited,
)

_D29_MORNING = datetime(2026, 6, 29, 9, 0, tzinfo=UTC)
_D29_EVENING = datetime(2026, 6, 29, 19, 40, tzinfo=UTC)
_D29_LAST_SECOND = datetime(2026, 6, 29, 23, 59, 59, tzinfo=UTC)
_D30_FIRST_SECOND = datetime(2026, 6, 30, 0, 0, 1, tzinfo=UTC)
# 01:00 at +03:00 is still 22:00Z on the 29th — the budget's day, not the reporter's.
_D30_LOCAL_BUT_D29_UTC = datetime(2026, 6, 30, 1, 0, tzinfo=timezone(timedelta(hours=3)))


@pytest.mark.parametrize(
    ("description", "recorded_at", "scrape_at", "expected_today", "expected_total"),
    [
        ("nothing recorded -> zero", [], _D29_EVENING, 0, 0),
        (
            "three ops in one UTC day -> all three count",
            [_D29_MORNING, _D29_EVENING, _D29_EVENING],
            _D29_EVENING,
            3,
            3,
        ),
        (
            "yesterday's ops -> off today's budget, still on the lifetime counter",
            [_D29_MORNING, _D29_EVENING],
            _D30_FIRST_SECOND,
            0,
            2,
        ),
        (
            "ops either side of the reset -> only the new day's counts today",
            [_D29_LAST_SECOND, _D30_FIRST_SECOND],
            _D30_FIRST_SECOND,
            1,
            2,
        ),
        (
            "op timestamped in a non-UTC offset -> bucketed by its UTC day",
            [_D30_LOCAL_BUT_D29_UTC],
            _D29_EVENING,
            1,
            1,
        ),
        (
            "quiet operator scraped after the reset -> today rolls over on READ, not just on write",
            [_D29_EVENING],
            datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
            0,
            1,
        ),
    ],
)
def test_ops_today_window(
    description: str,
    recorded_at: Sequence[datetime],
    scrape_at: datetime,
    expected_today: int,
    expected_total: int,
) -> None:
    metrics = KeyOpMetrics()
    for moment in recorded_at:
        metrics.record("create", moment)
    assert metrics.ops_today(scrape_at) == expected_today, description
    assert metrics.ops_total() == expected_total, description


def test_render_exposes_both_windows_per_op() -> None:
    metrics = KeyOpMetrics()
    metrics.record("create", _D29_EVENING)
    metrics.record("delete", _D29_EVENING)
    metrics.record("delete", _D29_EVENING)
    text = metrics.render(_D29_EVENING)

    assert "# TYPE openrouter_key_api_ops_total counter" in text
    assert "# TYPE openrouter_key_api_ops_today gauge" in text
    assert 'openrouter_key_api_ops_total{op="delete"} 2' in text
    assert 'openrouter_key_api_ops_today{op="create"} 1' in text
    # every op keeps a series even at zero, so #27's alert has something to threshold from t0
    for op in KEY_OPS:
        assert f'openrouter_key_api_ops_today{{op="{op}"}}' in text
    assert text.endswith("\n")  # Prometheus text format requires the trailing newline


def test_render_after_reset_reports_zero_today() -> None:
    metrics = KeyOpMetrics()
    metrics.record("create", _D29_EVENING)
    text = metrics.render(_D30_FIRST_SECOND)
    assert 'openrouter_key_api_ops_today{op="create"} 0' in text
    assert 'openrouter_key_api_ops_total{op="create"} 1' in text


@pytest.mark.parametrize(
    ("description", "limit_name", "expected_class"),
    [
        (
            "the incident limit -> the daily class the alert watches",
            "keys-modify-api-rpd-v2",
            "daily",
        ),
        ("a burst limit -> other (it clears on its own)", "keys-modify-api-rpm-v1", "other"),
        ("an unnamed 429 -> other", None, "other"),
    ],
)
def test_rate_limited_counter_uses_the_same_classification(
    description: str, limit_name: str | None, expected_class: str
) -> None:
    # one source of truth: the counter's class is decide_retry's verdict, so the metric can never
    # disagree with what the operator actually did (park vs back off).
    metrics = KeyOpMetrics()
    metrics.record_rate_limited(limit_name, _D29_EVENING)
    text = metrics.render(_D29_EVENING)
    assert f'openrouter_key_api_rate_limited_total{{limit_class="{expected_class}"}} 1' in text, (
        description
    )


class _RecordingPort:
    """Inner port that records the calls a MeteredPort forwarded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_key(self, key_hash: str) -> KeyState | None:
        self.calls.append("get")
        return KeyState(hash=key_hash, name="x", limit=1.0, reset_interval=None)

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey:
        self.calls.append("create")
        return MintedKey(hash="GKnew", value="sk-or-v1-fake")

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None:
        self.calls.append("update")

    def delete_key(self, key_hash: str) -> None:
        self.calls.append("delete")


class _AlwaysRateLimitedPort(_RecordingPort):
    def delete_key(self, key_hash: str) -> None:
        self.calls.append("delete")
        raise RateLimited("keys-modify-api-rpd-v2")


@pytest.mark.parametrize(
    ("description", "op", "expected"),
    [
        ("read", lambda p: p.get_key("GK1"), "get"),
        ("mint", lambda p: p.create_key("demo", 1.0, None), "create"),
        ("budget patch", lambda p: p.update_key("GK1", 1.0, None), "update"),
        ("delete", lambda p: p.delete_key("GK1"), "delete"),
    ],
)
def test_metered_port_counts_and_forwards_every_op(
    description: str, op: Callable[[OpenRouterPort], object], expected: str
) -> None:
    inner = _RecordingPort()
    metrics = KeyOpMetrics()
    port: OpenRouterPort = MeteredPort(inner, metrics, lambda: _D29_EVENING)

    op(port)

    assert inner.calls == [expected], description  # wrapping must not swallow the call
    assert f'openrouter_key_api_ops_today{{op="{expected}"}} 1' in metrics.render(_D29_EVENING)


def test_metered_port_counts_a_rejected_op_and_reraises() -> None:
    # a 429'd request still SPENT budget — counting only successes would under-read the very
    # dimension the alert exists to watch. And the error must reach the handler's park logic.
    inner = _AlwaysRateLimitedPort()
    metrics = KeyOpMetrics()
    port: OpenRouterPort = MeteredPort(inner, metrics, lambda: _D29_EVENING)

    with pytest.raises(RateLimited):
        port.delete_key("GK1")

    text = metrics.render(_D29_EVENING)
    assert 'openrouter_key_api_ops_today{op="delete"} 1' in text
    assert 'openrouter_key_api_rate_limited_total{limit_class="daily"} 1' in text


def test_metered_port_returns_the_inner_result() -> None:
    port: OpenRouterPort = MeteredPort(_RecordingPort(), KeyOpMetrics(), lambda: _D29_EVENING)
    assert port.create_key("demo", 1.0, ResetInterval.weekly).hash == "GKnew"
    state = port.get_key("GK1")
    assert state is not None and state.hash == "GK1"


# --- account balance gauge (issue #29) -------------------------------------------------------

# The figures the live probe on the issue returned for this operator's own provisioning key
# (`GET /api/v1/credits` -> 200, 2026-08-08): $50 granted, $29.83 spent, $20.17 left.
_LIVE = AccountCredits(total_credits=50.0, total_usage=29.832844979)
_TOPPED_UP = AccountCredits(total_credits=70.0, total_usage=29.832844979)
_DRAINED = AccountCredits(total_credits=50.0, total_usage=50.0)
_OVERSPENT = AccountCredits(total_credits=50.0, total_usage=51.25)

# Polls are five minutes apart — the Nth poll happens at _POLL_TIMES[N] whether it succeeds or not.
_POLL_TIMES = [datetime(2026, 6, 29, 9, 0, tzinfo=UTC) + timedelta(minutes=5 * i) for i in range(4)]
_TS_POLL_0 = "1782723600"  # unix seconds of _POLL_TIMES[0]
_TS_POLL_2 = "1782724200"  # ... and of _POLL_TIMES[2]


class _ScriptedCreditPort:
    """Account port replaying scripted poll outcomes; a `None` outcome means the call blew up."""

    def __init__(self, outcomes: Sequence[AccountCredits | None]) -> None:
        self._outcomes = list(outcomes)

    def get_account_credits(self) -> AccountCredits:
        outcome = self._outcomes.pop(0)
        if outcome is None:
            raise RuntimeError("credits endpoint unreachable")
        return outcome


def _clock_at(index: int) -> Callable[[], datetime]:
    return lambda: _POLL_TIMES[index]


def _drive_polls(outcomes: Sequence[AccountCredits | None]) -> str:
    """Run the scripted polls through the real collector and render the exposition text."""
    metrics = KeyOpMetrics()
    port: AccountPort = _ScriptedCreditPort(outcomes)
    for index in range(len(outcomes)):
        poll_account_credit(port, metrics.account, _clock_at(index))
    return metrics.render(_D29_EVENING)


@pytest.mark.parametrize(
    ("description", "outcomes", "expected_lines"),
    [
        (
            "never polled -> the series exists but carries no value; NaN, never 0",
            (),
            (
                "openrouter_account_credit_usd NaN",
                "openrouter_account_credit_updated_timestamp_seconds 0",
                "openrouter_account_credit_poll_failures_total 0",
            ),
        ),
        (
            "one good poll -> balance is total_credits minus total_usage",
            (_LIVE,),
            (
                "openrouter_account_credit_usd 20.167155",
                f"openrouter_account_credit_updated_timestamp_seconds {_TS_POLL_0}",
                "openrouter_account_credit_poll_failures_total 0",
            ),
        ),
        (
            "the poll fails before it ever succeeded -> still valueless, NOT a $0 balance",
            (None,),
            (
                "openrouter_account_credit_usd NaN",
                "openrouter_account_credit_updated_timestamp_seconds 0",
                "openrouter_account_credit_poll_failures_total 1",
            ),
        ),
        (
            "a failure after a good reading -> last known balance held, freshness pinned to it",
            (_LIVE, None),
            (
                "openrouter_account_credit_usd 20.167155",
                f"openrouter_account_credit_updated_timestamp_seconds {_TS_POLL_0}",
                "openrouter_account_credit_poll_failures_total 1",
            ),
        ),
        (
            "a later success supersedes the held value and moves freshness forward",
            (_LIVE, None, _TOPPED_UP),
            (
                "openrouter_account_credit_usd 40.167155",
                f"openrouter_account_credit_updated_timestamp_seconds {_TS_POLL_2}",
                "openrouter_account_credit_poll_failures_total 1",
            ),
        ),
        (
            "a genuinely drained account -> a real 0, which is why an absent reading cannot be 0",
            (_DRAINED,),
            (
                "openrouter_account_credit_usd 0",
                f"openrouter_account_credit_updated_timestamp_seconds {_TS_POLL_0}",
                "openrouter_account_credit_poll_failures_total 0",
            ),
        ),
        (
            "usage past the grant -> negative, not clamped (the account owes)",
            (_OVERSPENT,),
            (
                "openrouter_account_credit_usd -1.25",
                f"openrouter_account_credit_updated_timestamp_seconds {_TS_POLL_0}",
                "openrouter_account_credit_poll_failures_total 0",
            ),
        ),
    ],
)
def test_account_credit_gauge(
    description: str,
    outcomes: Sequence[AccountCredits | None],
    expected_lines: Sequence[str],
) -> None:
    text = _drive_polls(outcomes)
    for line in expected_lines:
        assert f"\n{line}\n" in f"\n{text}", f"{description}: missing {line!r} in\n{text}"


def test_a_fresh_instance_exposes_the_balance_series_for_the_chart_guard() -> None:
    """Mirrors ``tests/test_chart_metrics.py::test_every_alert_has_a_series_behind_it``, which
    renders a fresh, never-polled ``KeyOpMetrics`` and rejects any alert naming a metric that
    surface lacks. #33's low-water alert is written against this exact name, so the contract is
    asserted from this side too: the series is on the one renderer the guard reads, present from
    t0, and valueless (NaN) rather than 0 until a poll succeeds.
    """
    exposed = KeyOpMetrics().render(_D29_EVENING)
    assert "# TYPE openrouter_account_credit_usd gauge" in exposed
    assert "\nopenrouter_account_credit_usd NaN\n" in exposed
    assert exposed.endswith("\n")  # Prometheus text format requires the trailing newline


@pytest.mark.parametrize(
    ("description", "raw", "expected"),
    [
        ("unset -> 5 minutes; it is an account-scope number, not a per-reconcile one", None, 300.0),
        ("the chart can slow it down", "900", 900.0),
        ("below the floor -> floored, so a typo cannot hammer the key API", "5", 60.0),
        ("unparseable -> the default; a bad value must not crash operator startup", "5m", 300.0),
    ],
)
def test_credit_poll_interval(description: str, raw: str | None, expected: float) -> None:
    assert credit_poll_interval_s(raw) == expected, description
