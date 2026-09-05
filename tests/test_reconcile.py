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
from openrouter_operator.operator import RENEW_TIMER_INTERVAL_S
from openrouter_operator.ports import KeyState, MintedKey, OpenRouterPort, RateLimited, SecretState
from openrouter_operator.reconcile import (
    MIN_PARK_S,
    RENEW_THRESHOLD_S,
    RENEW_WINDOW,
    Backoff,
    Create,
    Desired,
    NoOp,
    NormalizeSecret,
    ParkUntilReset,
    Plan,
    RetryPlan,
    Rotate,
    SecretMissing,
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


def _secret_ok() -> SecretState:
    """A healthy Secret: exists, labeled, all three data keys present."""
    return SecretState(exists=True, has_label=True, has_all_keys=True)


def _secret_missing() -> SecretState:
    return SecretState(exists=False, has_label=False, has_all_keys=False)


def _secret_unlabeled() -> SecretState:
    return SecretState(exists=True, has_label=False, has_all_keys=True)


def _secret_shape_drifted() -> SecretState:
    return SecretState(exists=True, has_label=True, has_all_keys=False)


@pytest.mark.parametrize(
    ("description", "observed", "secret", "expected"),
    [
        ("no key yet -> create", None, _secret_missing(), Create),
        ("everything matches -> noop", _state(), _secret_ok(), NoOp),
        ("budget drift -> update", _state(limit=10.0), _secret_ok(), Update),
        ("reset drift -> update", _state(reset=ResetInterval.monthly), _secret_ok(), Update),
        # Secret drift (issue #53): upstream key healthy, but the k8s Secret is wrong
        # A missing Secret cannot be normalized (key value is only known at mint time),
        # so decide() returns the SecretMissing plan, which surfaces the unrecoverable state
        # (status condition + Prometheus metric) rather than acting on it (#56).
        (
            "Secret missing, key healthy -> SecretMissing (surface, #56)",
            _state(),
            _secret_missing(),
            SecretMissing,
        ),
        (
            "Secret unlabeled, key healthy -> normalize",
            _state(),
            _secret_unlabeled(),
            NormalizeSecret,
        ),
        (
            "Secret shape-drifted (missing data keys), key healthy -> normalize",
            _state(),
            _secret_shape_drifted(),
            NormalizeSecret,
        ),
    ],
)
def test_decide(
    description: str, observed: KeyState | None, secret: SecretState, expected: type[Plan]
) -> None:
    plan = decide(DESIRED, observed, secret, NOW, secret_name="sleep-tracking-openrouter")
    assert isinstance(plan, expected), description
    if isinstance(plan, Update):
        assert observed is not None
        assert plan.key_hash == observed.hash
        assert plan.desired == DESIRED
    if isinstance(plan, SecretMissing):
        assert plan.secret_name == "sleep-tracking-openrouter", description


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
    usage: float | None = None,
) -> KeyState:
    """Observed session key: minted with no reset window -> reset_interval is None (must NOT
    read back as weekly, or decide() update-loops it forever). `usage` defaults to None = the
    read did not report spend, which is what makes an age renewal skip rather than guess (#25)."""
    return KeyState(
        hash="GKsess",
        name="sleep-tracking-session-issue-42-round-1",
        limit=limit,
        reset_interval=None,
        expires_at=expires_at,
        disabled=disabled,
        usage=usage,
    )


_PAST = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)  # before NOW (11:00) — expired
_FUTURE = datetime(2026, 6, 29, 12, 30, tzinfo=UTC)  # after NOW — still live


# The desired ephemeral expiry (12:00) ± tolerance: OpenRouter stores a requested instant rounded
# by seconds, so "matches" means within EXPIRY_TOLERANCE_S, not equality (or every reconcile would
# rotate). _DESIRED_EXP_STORED simulates that rounding; _EXTENDED is a genuine re-mint extension.
_DESIRED_EXP_STORED = datetime(2026, 6, 29, 11, 59, 58, tzinfo=UTC)  # 12:00 as OpenRouter stored it
_EXTENDED = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)  # live key already lasts LONGER than desired


@pytest.mark.parametrize(
    ("description", "observed", "secret", "expected"),
    [
        ("no session key yet -> create", None, _secret_missing(), Create),
        (
            "session key live, cap + expiry match (within storage rounding) -> noop",
            _eph_state(expires_at=_DESIRED_EXP_STORED),
            _secret_ok(),
            NoOp,
        ),
        (
            "session cap drift -> update",
            _eph_state(limit=1.0, expires_at=_DESIRED_EXP_STORED),
            _secret_ok(),
            Update,
        ),
        # issue #6 (proven live 2026-07-09): PATCH cannot change expires_at, so expiry drift ROTATES
        # (mint+swap+delete). A re-minted CR extending a session used to reconcile "successfully"
        # while the real key kept its original deadline — and a healthy run died at it.
        (
            "expiry drift beyond tolerance (12:30 vs desired 12:00) -> rotate",
            _eph_state(expires_at=_FUTURE),
            _secret_ok(),
            Rotate,
        ),
        (
            "larger drift (+2h vs desired) -> rotate",
            _eph_state(expires_at=_EXTENDED),
            _secret_ok(),
            Rotate,
        ),
        (
            "no expiry on live key, spec wants one -> rotate",
            _eph_state(expires_at=None),
            _secret_ok(),
            Rotate,
        ),
        # expiry drift OUTRANKS cap drift: an Update would PATCH the cap and keep the stale
        # deadline — the whole issue-#6 failure shape.
        (
            "expiry + cap both drifted -> rotate (not update)",
            _eph_state(limit=1.0, expires_at=_FUTURE),
            _secret_ok(),
            Rotate,
        ),
        # self-heal: a dead key (the '401 User not found' corpse) must re-mint, not NoOp
        (
            "session key EXPIRED -> recreate",
            _eph_state(expires_at=_PAST),
            _secret_missing(),
            Create,
        ),
        (
            "session key REVOKED (disabled) -> recreate",
            _eph_state(disabled=True),
            _secret_missing(),
            Create,
        ),
        # Secret drift on ephemeral keys (issue #53)
        # A missing Secret cannot be normalized (key value is only known at mint time),
        # so decide() returns the SecretMissing plan, which surfaces the unrecoverable state
        # (status condition + Prometheus metric) rather than acting on it (#56).
        (
            "session key healthy, Secret missing -> SecretMissing (surface, #56)",
            _eph_state(expires_at=_DESIRED_EXP_STORED),
            _secret_missing(),
            SecretMissing,
        ),
        (
            "session key healthy, Secret unlabeled -> normalize",
            _eph_state(expires_at=_DESIRED_EXP_STORED),
            _secret_unlabeled(),
            NormalizeSecret,
        ),
        (
            "session key healthy, Secret shape-drifted -> normalize",
            _eph_state(expires_at=_DESIRED_EXP_STORED),
            _secret_shape_drifted(),
            NormalizeSecret,
        ),
    ],
)
def test_decide_ephemeral(
    description: str, observed: KeyState | None, secret: SecretState, expected: type[Plan]
) -> None:
    plan = decide(
        EPHEMERAL_DESIRED,
        observed,
        secret,
        NOW,
        secret_name="sleep-tracking-session-issue-42-round-1-openrouter",
    )
    assert isinstance(plan, expected), description
    if isinstance(plan, SecretMissing):
        assert plan.secret_name == "sleep-tracking-session-issue-42-round-1-openrouter", description


def test_rotate_skipped_when_desired_expiry_already_past() -> None:
    # live key, expiry drifted, but the DESIRED expiry is itself past → never rotate on that drift:
    # a rotation honouring it would mint a born-dead key. Since #25 a past deadline is inside the
    # renewal window, so the age decision answers this instead — and answers it the same way,
    # because the live key (12:30) is nowhere near its own expiry at 11:00. NoOp either way.
    stale = Desired(name="x", limit=0.5, reset_interval=None, expires_at=_PAST)
    assert isinstance(
        decide(stale, _eph_state(expires_at=_FUTURE), _secret_ok(), NOW, secret_name="x"), NoOp
    )


def test_decide_skips_born_dead_remint() -> None:
    # dead key AND the spec's own expiresAt is already past → a re-mint would be born-dead and
    # hot-loop, so NoOp and wait for a fresh CR (new round) instead.
    stale = Desired(name="x", limit=0.5, reset_interval=None, expires_at=_PAST)
    assert isinstance(
        decide(stale, _eph_state(expires_at=_PAST), _secret_ok(), NOW, secret_name="x"), NoOp
    )
    # never minted + already-past spec -> NoOp
    assert isinstance(decide(stale, None, _secret_ok(), NOW, secret_name="x"), NoOp)


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


# ── Proactive rotation before a key dies of AGE (issue #25) ─────────────────────────────────────
# Rotation used to be drift-triggered ONLY: decide() compared the spec's expiry against the live
# key's, so nothing ever watched a key's own clock. A ride on a key with a perfectly honest window
# simply outlasted it (sleep-tracking#96: laguna:free at ~306s/turn hit the 2h mint TTL), the worker
# 401-stormed and the egress proxy opened its auth circuit for 900s. Whole ride lost.
#
# The fix: once the clock is within RENEW_THRESHOLD_S of the spec's deadline, THIS operator owns the
# key's lifetime — it re-mints before the live key dies (Rotate: mint + Secret swap, which the
# proxy's per-request `ref:` resolution picks up within its 60s cache, no pod restart) and stops
# measuring the key against a deadline it has deliberately moved past.
#
# Three things this table pins, because each is a way the fix could become a regression:
#   * the renewal carries the REMAINING budget (live cap − spend), never a fresh `budgetUSD` — a
#     rotation that resets spend is a breaker regression, not a fix (the `_is_dead` reasoning);
#   * spend it cannot read, or spend that already ate the cap, means NO renewal (fail-safe both
#     ways: the key then dies of age exactly as it does today, rather than being resurrected);
#   * a renewed key must not re-trigger anything — not the age check (it mints a fresh window) and
#     not the drift check (its expiry now legitimately outlives the spec's), or reconcile hot-loops.
# The old key is deliberately NOT deleted on this path: it is seconds-to-minutes from lapsing on its
# own, and deleting it early would 401 any proxy instance still holding a ≤60s-stale cached ref.

_RENEW_NOW = datetime(2026, 6, 29, 11, 45, tzinfo=UTC)  # 15 min before the spec deadline (12:00)
_RENEWED_EXP = _RENEW_NOW + RENEW_WINDOW  # the window a renewal at _RENEW_NOW mints


@pytest.mark.parametrize(
    ("description", "desired", "observed", "now", "expected", "expected_limit", "deletes_old"),
    [
        (
            "live session key 15 min from its expiry, $0.20 spent -> renew on the remainder",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=_DESIRED_EXP_STORED, usage=0.2),
            _RENEW_NOW,
            Rotate,
            0.3,
            False,
        ),
        (
            "renewal carries the LIVE key's cap, not the spec's -> a 2nd renewal chains down",
            EPHEMERAL_DESIRED,
            _eph_state(limit=0.3, expires_at=_RENEWED_EXP, usage=0.1),
            _RENEWED_EXP - timedelta(minutes=10),
            Rotate,
            0.2,
            False,
        ),
        (
            "spend unknown (the read did not report usage) -> skip the pass, never guess $0",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=_DESIRED_EXP_STORED, usage=None),
            _RENEW_NOW,
            NoOp,
            None,
            False,
        ),
        (
            "cap already spent -> no renewal (an exhausted key 403s by design)",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=_DESIRED_EXP_STORED, usage=0.5),
            _RENEW_NOW,
            NoOp,
            None,
            False,
        ),
        (
            "spend past the cap -> still no renewal",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=_DESIRED_EXP_STORED, usage=0.6),
            _RENEW_NOW,
            NoOp,
            None,
            False,
        ),
        (
            "live key nowhere near its expiry -> untouched (the trigger is age, not every pass)",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=_DESIRED_EXP_STORED, usage=0.2),
            NOW,  # 11:00 — a full hour of window left
            NoOp,
            None,
            False,
        ),
        (
            "just renewed, spec deadline still ahead -> noop (the fresh window cleared the "
            "trigger; re-rotating on 'drift' against the spec's deadline would hot-loop)",
            EPHEMERAL_DESIRED,
            _eph_state(limit=0.3, expires_at=_RENEWED_EXP, usage=0.2),
            datetime(2026, 6, 29, 11, 50, tzinfo=UTC),
            NoOp,
            None,
            False,
        ),
        (
            "renewed key running past the spec deadline -> noop, it is the live key that matters",
            EPHEMERAL_DESIRED,
            _eph_state(limit=0.3, expires_at=_RENEWED_EXP, usage=0.2),
            datetime(2026, 6, 29, 12, 30, tzinfo=UTC),  # spec said 12:00; the ride is still up
            NoOp,
            None,
            False,
        ),
        (
            "live key carries no expiry at all -> nothing to renew (it cannot die of age)",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=None, usage=0.2),
            _RENEW_NOW,
            NoOp,
            None,
            False,
        ),
        (
            "standing project key -> never age-renewed (no deadline; it is the funding ceiling)",
            DESIRED,
            _state(),
            _RENEW_NOW,
            NoOp,
            None,
            False,
        ),
        (
            "expiry DRIFT is unchanged: full spec cap, and the old key still gets deleted (#6)",
            EPHEMERAL_DESIRED,
            _eph_state(expires_at=_FUTURE, usage=0.2),
            NOW,
            Rotate,
            0.5,
            True,
        ),
    ],
)
def test_decide_renews_before_age_death(
    description: str,
    desired: Desired,
    observed: KeyState,
    now: datetime,
    expected: type[Plan],
    expected_limit: float | None,
    deletes_old: bool,
) -> None:
    plan = decide(desired, observed, _secret_ok(), now, secret_name="x")
    assert isinstance(plan, expected), description
    if isinstance(plan, Rotate):
        assert plan.key_hash == observed.hash, description
        assert plan.desired.limit == pytest.approx(expected_limit), description
        assert plan.delete_old is deletes_old, description
        if plan.delete_old:  # drift rotation: the spec's own deadline, honoured as written
            assert plan.desired.expires_at == desired.expires_at, description
        else:  # age renewal: a FRESH window, or the fix would re-trigger itself every pass
            assert plan.desired.expires_at == now + RENEW_WINDOW, description


def test_renewal_chain_caps_total_spend_across_rotations() -> None:
    """The acceptance criterion of #25, as an invariant over a whole chain: a ride that outlives
    its key's original window always has a live credential, and total spend across old+new keys
    never exceeds the ONE `budgetUSD` the CR asked for. Each renewal hands on what is left, so the
    chain converges on an exhausted cap — it does not renew its way to unlimited spend.
    """
    budget = EPHEMERAL_DESIRED.limit  # 0.5
    burn_per_key = 0.1
    spent = 0.0
    limit, expires = budget, _DESIRED_EXP_STORED

    for _ in range(4):
        now = expires - timedelta(minutes=10)  # the ride is still running; the key is nearly dead
        plan = decide(
            EPHEMERAL_DESIRED,
            _eph_state(limit=limit, expires_at=expires, usage=burn_per_key),
            _secret_ok(),
            now,
            secret_name="x",
        )
        assert isinstance(plan, Rotate) and plan.delete_old is False
        renewed = plan.desired
        assert renewed.expires_at is not None  # never a window without a live credential
        assert renewed.expires_at > now
        spent += burn_per_key
        limit, expires = renewed.limit, renewed.expires_at
        assert spent + limit == pytest.approx(budget)  # ...and never more cap than was funded

    # The last of the budget burns: no renewal at all, the session ends on an exhausted key.
    last = decide(
        EPHEMERAL_DESIRED,
        _eph_state(limit=limit, expires_at=expires, usage=limit),
        _secret_ok(),
        expires - timedelta(minutes=10),
        secret_name="x",
    )
    assert isinstance(last, NoOp)


def test_renewal_timer_fires_at_least_twice_inside_the_window() -> None:
    """The renewal decision can only act on a pass that actually happens, and nothing generates an
    event when a key ages — so the timer's cadence is load-bearing (issue #25). If its interval
    ever grew past the threshold, a key would sail through the whole window untouched and die of
    age again: the bug reintroduced by a number rather than by logic. The timer itself is I/O glue,
    but the relationship between the two constants is exactly the kind of thing a table can pin.
    """
    assert 0 < RENEW_TIMER_INTERVAL_S * 2 <= RENEW_THRESHOLD_S


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
