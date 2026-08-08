"""Decision-table tests for the key-API op telemetry (issue #26, deliverable b).

The daily key-op budget was an untracked capacity dimension: today's record ride volume × (mint +
budget patch + delete) exhausted a limit nobody measured, and the crosscheck read "healthy" because
no alert exists for it. `KeyOpMetrics` counts what the operator actually spends against that
budget. The table pins the part that is easy to get wrong: the counting window is the UTC calendar
day — the same window the rpd limit resets on — so a scrape must never report yesterday's ops as
today's, and the rollover has to happen on READ as well as on write (a quiet operator still scrapes
every 30s, and a gauge stuck at yesterday's total would fire the threshold alert forever).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from openrouter_operator.metrics import KEY_OPS, KeyOpMetrics, MeteredPort
from openrouter_operator.models import ResetInterval
from openrouter_operator.ports import KeyState, MintedKey, OpenRouterPort, RateLimited

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
