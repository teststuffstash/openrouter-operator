"""Decision-table tests for the pure reconcile logic — the homelab testing doctrine: one
parametrized table of (observed key x desired spec) -> expected Plan, reviewable at a glance,
offline, no SDK. A reviewer can see a missing case in the table.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from openrouter_operator.adapter import OpenRouterAdapter, _to_state
from openrouter_operator.metrics import KeyOpMetrics, MeteredPort
from openrouter_operator.models import OpenRouterKeySpec, ResetInterval
from openrouter_operator.ports import KeyState, MintedKey, OpenRouterPort, RateLimited
from openrouter_operator.reconcile import (
    MIN_PARK_S,
    Backoff,
    Create,
    Desired,
    NoOp,
    ParkUntilReset,
    Plan,
    RetryPlan,
    Rotate,
    Update,
    decide,
    decide_retry,
    desired_from_spec,
    should_collect,
)

SPEC = OpenRouterKeySpec.model_validate(
    {
        "project": "sleep-tracking",
        "budgetUSD": 5.0,
        "resetInterval": "weekly",
        "guardrail": "only-free",
    }
)
DESIRED = desired_from_spec(SPEC)

# Fixed "now" for the decision table. Before EPHEMERAL_SPEC's expiresAt (12:00) so an ephemeral mint
# produces a live key; PAST/FUTURE observed expiries are set relative to it.
NOW = datetime(2026, 6, 29, 11, 0, tzinfo=UTC)


def _state(
    *,
    limit: float = 5.0,
    reset: ResetInterval = ResetInterval.weekly,
) -> KeyState:
    return KeyState(hash="GK1", name="sleep-tracking-agent", limit=limit, reset_interval=reset)


@pytest.mark.parametrize(
    ("description", "observed", "expected"),
    [
        ("no key yet -> create", None, Create),
        ("everything matches -> noop", _state(), NoOp),
        ("budget drift -> update", _state(limit=10.0), Update),
        ("reset drift -> update", _state(reset=ResetInterval.monthly), Update),
    ],
)
def test_decide(description: str, observed: KeyState | None, expected: type[Plan]) -> None:
    plan = decide(DESIRED, observed, NOW)
    assert isinstance(plan, expected), description
    if isinstance(plan, Update):
        assert observed is not None
        assert plan.key_hash == observed.hash
        assert plan.desired == DESIRED


def test_desired_from_spec_maps_fields() -> None:
    assert DESIRED.name == "sleep-tracking-agent"
    assert DESIRED.limit == 5.0
    assert DESIRED.reset_interval is ResetInterval.weekly


def test_spec_defaults_and_helpers() -> None:
    minimal = OpenRouterKeySpec.model_validate({"project": "demo", "budgetUSD": 1.0})
    assert minimal.reset_interval is ResetInterval.weekly  # weekly by default (blast-radius cap)
    assert minimal.guardrail is None
    assert minimal.target_secret_name() == "demo-openrouter"
    assert minimal.key_name() == "demo-agent"
    explicit = OpenRouterKeySpec.model_validate(
        {"project": "demo", "budgetUSD": 1.0, "secretName": "custom"}
    )
    assert explicit.target_secret_name() == "custom"


def test_spec_rejects_nonpositive_budget() -> None:
    with pytest.raises(ValueError):
        OpenRouterKeySpec.model_validate({"project": "demo", "budgetUSD": 0})


# ── Ephemeral session keys ─────────────────────────────────────────────────────────────────────
# A per-session key is the real budget breaker: HARD cap, no reset window (reset_interval=None),
# unique name + secret per session, optional self-destruct via expiresAt.

EPHEMERAL_SPEC = OpenRouterKeySpec.model_validate(
    {
        "project": "sleep-tracking",
        "budgetUSD": 0.5,
        "ephemeral": True,
        "session": "issue-42-round-1",
        "expiresAt": "2026-06-29T12:00:00Z",
    }
)
EPHEMERAL_DESIRED = desired_from_spec(EPHEMERAL_SPEC)


def _eph_state(
    *,
    limit: float = 0.5,
    expires_at: datetime | None = None,
    disabled: bool = False,
) -> KeyState:
    """Observed session key: minted with no reset window -> reset_interval is None (must NOT
    read back as weekly, or decide() update-loops it forever)."""
    return KeyState(
        hash="GKsess",
        name="sleep-tracking-session-issue-42-round-1",
        limit=limit,
        reset_interval=None,
        expires_at=expires_at,
        disabled=disabled,
    )


_PAST = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)  # before NOW (11:00) — expired
_FUTURE = datetime(2026, 6, 29, 12, 30, tzinfo=UTC)  # after NOW — still live


# The desired ephemeral expiry (12:00) ± tolerance: OpenRouter stores a requested instant rounded
# by seconds, so "matches" means within EXPIRY_TOLERANCE_S, not equality (or every reconcile would
# rotate). _DESIRED_EXP_STORED simulates that rounding; _EXTENDED is a genuine re-mint extension.
_DESIRED_EXP_STORED = datetime(2026, 6, 29, 11, 59, 58, tzinfo=UTC)  # 12:00 as OpenRouter stored it
_EXTENDED = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)  # live key already lasts LONGER than desired


@pytest.mark.parametrize(
    ("description", "observed", "expected"),
    [
        ("no session key yet -> create", None, Create),
        (
            "session key live, cap + expiry match (within storage rounding) -> noop",
            _eph_state(expires_at=_DESIRED_EXP_STORED),
            NoOp,
        ),
        (
            "session cap drift -> update",
            _eph_state(limit=1.0, expires_at=_DESIRED_EXP_STORED),
            Update,
        ),
        # issue #6 (proven live 2026-07-09): PATCH cannot change expires_at, so expiry drift ROTATES
        # (mint+swap+delete). A re-minted CR extending a session used to reconcile "successfully"
        # while the real key kept its original deadline — and a healthy run died at it.
        (
            "expiry drift beyond tolerance (12:30 vs desired 12:00) -> rotate",
            _eph_state(expires_at=_FUTURE),
            Rotate,
        ),
        ("larger drift (+2h vs desired) -> rotate", _eph_state(expires_at=_EXTENDED), Rotate),
        ("no expiry on live key, spec wants one -> rotate", _eph_state(expires_at=None), Rotate),
        # expiry drift OUTRANKS cap drift: an Update would PATCH the cap and keep the stale
        # deadline — the whole issue-#6 failure shape.
        (
            "expiry + cap both drifted -> rotate (not update)",
            _eph_state(limit=1.0, expires_at=_FUTURE),
            Rotate,
        ),
        # self-heal: a dead key (the '401 User not found' corpse) must re-mint, not NoOp
        ("session key EXPIRED -> recreate", _eph_state(expires_at=_PAST), Create),
        ("session key REVOKED (disabled) -> recreate", _eph_state(disabled=True), Create),
    ],
)
def test_decide_ephemeral(
    description: str, observed: KeyState | None, expected: type[Plan]
) -> None:
    plan = decide(EPHEMERAL_DESIRED, observed, NOW)
    assert isinstance(plan, expected), description


def test_rotate_skipped_when_desired_expiry_already_past() -> None:
    # live key, expiry drifted, but the DESIRED expiry is itself past → rotating would mint a
    # born-dead key (same guard as the re-mint path); NoOp until a fresh CR arrives.
    stale = Desired(name="x", limit=0.5, reset_interval=None, expires_at=_PAST)
    assert isinstance(decide(stale, _eph_state(expires_at=_FUTURE), NOW), NoOp)


def test_decide_skips_born_dead_remint() -> None:
    # dead key AND the spec's own expiresAt is already past → a re-mint would be born-dead and
    # hot-loop, so NoOp and wait for a fresh CR (new round) instead.
    stale = Desired(name="x", limit=0.5, reset_interval=None, expires_at=_PAST)
    assert isinstance(decide(stale, _eph_state(expires_at=_PAST), NOW), NoOp)
    assert isinstance(decide(stale, None, NOW), NoOp)  # never minted + already-past spec -> NoOp


def test_ephemeral_desired_and_helpers() -> None:
    # no reset window, unique name + secret, expiry carried through to mint time
    assert EPHEMERAL_DESIRED.name == "sleep-tracking-session-issue-42-round-1"
    assert EPHEMERAL_DESIRED.limit == 0.5
    assert EPHEMERAL_DESIRED.reset_interval is None
    assert EPHEMERAL_DESIRED.expires_at == datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
    assert EPHEMERAL_SPEC.effective_reset() is None
    assert (
        EPHEMERAL_SPEC.target_secret_name() == "sleep-tracking-session-issue-42-round-1-openrouter"
    )


def test_ephemeral_requires_session() -> None:
    with pytest.raises(ValueError):
        OpenRouterKeySpec.model_validate({"project": "demo", "budgetUSD": 0.5, "ephemeral": True})


# ── GC of expired ephemeral keys (issue #10) ────────────────────────────────────────────────────
# An ephemeral session key self-destructs server-side at expiresAt, but the CR + its Secret linger
# forever (the spec never changes when the clock passes expiresAt, so no reconcile event fires).
# A periodic timer collects them: ephemeral ∧ now > expiresAt + 24h grace → collect; a standing key
# is NEVER collected (it's the funding ceiling). The grace window is testable here without a clock.

_EPH_GC_SPEC = OpenRouterKeySpec.model_validate(
    {
        "project": "sleep-tracking",
        "budgetUSD": 0.5,
        "ephemeral": True,
        "session": "issue-42-round-1",
        "expiresAt": "2026-06-29T12:00:00Z",
    }
)
_STANDING_GC_SPEC = OpenRouterKeySpec.model_validate(
    {"project": "sleep-tracking", "budgetUSD": 5.0, "resetInterval": "weekly"}
)
_EPH_NO_EXP_SPEC = OpenRouterKeySpec.model_validate(
    {
        "project": "sleep-tracking",
        "budgetUSD": 0.5,
        "ephemeral": True,
        "session": "issue-42-round-1",
    }
)
_GC_EXPIRY = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("description", "spec", "now", "expected"),
    [
        # the negative case the issue demands: a standing key must never be collected
        ("standing key never collected", _STANDING_GC_SPEC, _GC_EXPIRY, False),
        ("ephemeral, not yet expired -> keep", _EPH_GC_SPEC, _GC_EXPIRY, False),
        (
            "ephemeral, expired but within 24h grace -> keep",
            _EPH_GC_SPEC,
            datetime(2026, 6, 30, 11, 0, tzinfo=UTC),  # +23h
            False,
        ),
        (
            "ephemeral, expired past 24h grace -> collect",
            _EPH_GC_SPEC,
            datetime(2026, 6, 30, 13, 0, tzinfo=UTC),  # +25h
            True,
        ),
        ("ephemeral, no expiresAt -> keep", _EPH_NO_EXP_SPEC, _GC_EXPIRY, False),
    ],
)
def test_should_collect(
    description: str, spec: OpenRouterKeySpec, now: datetime, expected: bool
) -> None:
    assert should_collect(spec, now) is expected, description


def test_to_state_preserves_null_reset() -> None:
    # a no-reset (session) key must map to reset_interval=None, not a weekly default
    no_reset = _to_state(SimpleNamespace(hash="GK", name="x", limit=0.5, limit_reset=None))
    assert no_reset.reset_interval is None
    weekly = _to_state(SimpleNamespace(hash="GK", name="x", limit=5.0, limit_reset="weekly"))
    assert weekly.reset_interval is ResetInterval.weekly


def test_to_state_parses_liveness() -> None:
    # expires_at (ISO string or datetime) + disabled feed the dead-key self-heal
    s = _to_state(
        SimpleNamespace(
            hash="GK",
            name="x",
            limit=0.5,
            limit_reset=None,
            expires_at="2026-06-29T12:00:00Z",
            disabled=True,
        )
    )
    assert s.expires_at == datetime(2026, 6, 29, 12, 0, tzinfo=UTC) and s.disabled is True
    # absent attrs default safely (a key with no expiry / not disabled)
    bare = _to_state(SimpleNamespace(hash="GK", name="x", limit=0.5, limit_reset="weekly"))
    assert bare.expires_at is None and bare.disabled is False


class _FakePort:
    """A fake OpenRouterPort — proves the Protocol is satisfiable and is the testing seam for
    handler-level tests later (mock the port, never the live API)."""

    def get_key(self, key_hash: str) -> KeyState | None:
        return None

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey:
        return MintedKey(hash="GKnew", value="sk-or-v1-fake")

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None:
        return None

    def delete_key(self, key_hash: str) -> None:
        return None


def test_fake_port_satisfies_protocol() -> None:
    port: OpenRouterPort = _FakePort()
    assert port.get_key("x") is None
    minted = port.create_key("demo-agent", 1.0, ResetInterval.weekly)
    assert minted.value.startswith("sk-or-")


# ── rpd-class 429 -> park until the UTC reset (issue #26) ───────────────────────────────────────
# `keys-modify-api-rpd-v2` is a requests-per-DAY limit on the key API. Once it is exhausted NO
# retry can succeed until the counter rolls over at UTC midnight, but kopf's default exponential
# backoff hot-retried it anyway (~28/min fleet-wide, 434 429s in 15 min) and wedged 13 deletions on
# their finalizers. So the retry decision is a pure function of the limit the port reports — a
# per-MINUTE limit must still back off normally (parking that for hours would stall reconcile),
# and an unnamed 429 is not provably daily. The SDK's exception classes never leave adapter.py:
# the port raises a typed `RateLimited`, and this table is the whole classification.

_INCIDENT_LIMIT = "keys-modify-api-rpd-v2"
_RL_NOW = datetime(2026, 6, 29, 19, 40, tzinfo=UTC)  # 4h20m (15600s) before the UTC reset


@pytest.mark.parametrize(
    ("description", "limit_name", "now", "expected", "expected_delay"),
    [
        (
            "the incident limit (rpd) -> park until the UTC reset",
            _INCIDENT_LIMIT,
            _RL_NOW,
            ParkUntilReset,
            15600.0,
        ),
        (
            "requests-per-day spelled out, mixed case -> park",
            "Requests-Per-Day quota exhausted",
            _RL_NOW,
            ParkUntilReset,
            15600.0,
        ),
        (
            "per-MINUTE burst limit -> plain backoff (a 4h park would stall reconcile)",
            "keys-modify-api-rpm-v1",
            _RL_NOW,
            Backoff,
            None,
        ),
        (
            "429 the API did not name -> backoff (not provably daily)",
            None,
            _RL_NOW,
            Backoff,
            None,
        ),
        (
            "rpd 429 seconds before the reset -> floored, never a 0s hot spin",
            _INCIDENT_LIMIT,
            datetime(2026, 6, 29, 23, 59, 30, tzinfo=UTC),
            ParkUntilReset,
            MIN_PARK_S,
        ),
        (
            "clock reported in a non-UTC offset -> reset is still UTC midnight",
            _INCIDENT_LIMIT,
            datetime(2026, 6, 29, 19, 40, tzinfo=timezone(timedelta(hours=3))),  # = 16:40Z
            ParkUntilReset,
            26400.0,
        ),
    ],
)
def test_decide_retry(
    description: str,
    limit_name: str | None,
    now: datetime,
    expected: type[RetryPlan],
    expected_delay: float | None,
) -> None:
    plan = decide_retry(limit_name, now)
    assert isinstance(plan, expected), description
    if isinstance(plan, ParkUntilReset):
        assert plan.delay_s == expected_delay, description


class _RpdLimitedPort:
    """A fake port in the incident's shape: every key-MODIFY op hits the exhausted daily limit.

    Reads are unaffected — it was create/patch/delete that hammered, and `delete_key` specifically
    that wedged 13 CRs on their finalizers.
    """

    def get_key(self, key_hash: str) -> KeyState | None:
        return _state()

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey:
        raise RateLimited(_INCIDENT_LIMIT)

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None:
        raise RateLimited(_INCIDENT_LIMIT)

    def delete_key(self, key_hash: str) -> None:
        raise RateLimited(_INCIDENT_LIMIT)


@pytest.mark.parametrize(
    ("description", "op"),
    [
        ("create (the mint every dispatch defers on)", lambda p: p.create_key("demo", 1.0, None)),
        ("update (the budget patch)", lambda p: p.update_key("GK1", 1.0, None)),
        ("delete (the path that wedged 13 finalizers)", lambda p: p.delete_key("GK1")),
    ],
)
def test_every_key_modify_path_parks(
    description: str, op: Callable[[OpenRouterPort], object]
) -> None:
    port: OpenRouterPort = _RpdLimitedPort()
    with pytest.raises(RateLimited) as caught:
        op(port)
    assert isinstance(decide_retry(caught.value.limit_name, _RL_NOW), ParkUntilReset), description


def test_read_path_is_untouched() -> None:
    # the park is for key-MODIFY ops; a read still returns state (no blanket outage)
    assert _RpdLimitedPort().get_key("GK1") is not None


# ── delete is idempotent: an upstream 404 IS deleted (issue #30) ────────────────────────────────
# The #26 park drained the rpd-wedged deletions, but 11 CRs stayed wedged on a SECOND class: their
# keys no longer exist upstream (deleted mid-storm, or self-destructed at expiry), so every delete
# retry 404s, the SDK error propagates, kopf backs off forever and the finalizer never clears. A
# 404 on delete IS success — the desired state ("key absent upstream") already holds, which is the
# whole port contract for a delete. The translation is deliberately DELETE-scoped: a 404 on
# get/update means the key vanished under us mid-reconcile and must still surface.
#
# Same seam and same duck-typing as the #26 429 translation, so this table is the classification:
# what counts as "already gone" vs what still reaches kopf's backoff.


class _NotFoundResponseError(Exception):
    """The incident error, by shape: the beta SDK raised
    `openrouter.errors.notfoundresponse_error.NotFoundResponseError: API key not found`, and the
    operator log carries no HTTP status alongside it — so this row is matched by class NAME."""


class _HTTPError(Exception):
    """An SDK error that names its HTTP status the way `_call`'s 429 translation reads it."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _adapter_deleting(raises: Exception | None) -> tuple[OpenRouterAdapter, list[str]]:
    """A REAL `OpenRouterAdapter` wired to a fake SDK client, so the table exercises the actual
    translation rather than a restatement of it. Built without `__init__` on purpose: the
    `openrouter` extra is deliberately not installed for CI, and what is under test here is our
    error handling, never the SDK's."""
    seen: list[str] = []

    def delete(*, hash: str) -> None:  # kwarg name is the SDK's (keyword-only `hash=`)
        seen.append(hash)
        if raises is not None:
            raise raises

    adapter = object.__new__(OpenRouterAdapter)
    adapter._client = SimpleNamespace(api_keys=SimpleNamespace(delete=delete))
    return adapter, seen


@pytest.mark.parametrize(
    ("description", "raises", "expected"),
    [
        ("key exists upstream -> deleted", None, None),
        (
            "absent upstream (the incident NotFound) -> success, finalizer clears",
            _NotFoundResponseError("API key not found"),
            None,
        ),
        ("absent upstream, error names HTTP 404 -> success", _HTTPError(404), None),
        (
            "rpd 429 -> still RateLimited (the #26 park is untouched)",
            _HTTPError(429),
            RateLimited,
        ),
        ("upstream 500 -> propagates to kopf backoff", _HTTPError(500), _HTTPError),
        (
            "unrecognised error -> propagates (never silently swallowed)",
            RuntimeError("boom"),
            RuntimeError,
        ),
    ],
)
def test_delete_key_is_idempotent(
    description: str, raises: Exception | None, expected: type[Exception] | None
) -> None:
    adapter, seen = _adapter_deleting(raises)
    if expected is None:
        adapter.delete_key("GKgone")  # returns normally == the desired state already holds
    else:
        with pytest.raises(expected):
            adapter.delete_key("GKgone")
    assert seen == ["GKgone"], description  # the delete was attempted, not skipped


def test_absent_key_delete_still_counts_as_a_spent_op() -> None:
    """A 404'd delete still spent a key-API request, so it stays counted under `op="delete"` (#28's
    meter) — the count is honest even though nothing was deleted. And it must NOT land in the
    rate-limited series: that one is the parked-429 signal, not "this op did not do work"."""
    adapter, _ = _adapter_deleting(_NotFoundResponseError("API key not found"))
    metrics = KeyOpMetrics()
    port: OpenRouterPort = MeteredPort(adapter, metrics, lambda: NOW)

    port.delete_key("GKgone")  # returns: kopf's delete handler completes, the finalizer clears

    rendered = metrics.render(NOW)
    assert 'openrouter_key_api_ops_today{op="delete"} 1' in rendered
    assert 'openrouter_key_api_rate_limited_total{limit_class="daily"} 0' in rendered
