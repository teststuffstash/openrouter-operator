"""Decision-table tests for the pure reconcile logic — the homelab testing doctrine: one
parametrized table of (observed key x desired spec) -> expected Plan, reviewable at a glance,
offline, no SDK. A reviewer can see a missing case in the table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from openrouter_operator.adapter import _to_state
from openrouter_operator.models import OpenRouterKeySpec, ResetInterval
from openrouter_operator.ports import KeyState, MintedKey, OpenRouterPort
from openrouter_operator.reconcile import (
    Create,
    Desired,
    NoOp,
    Plan,
    Update,
    decide,
    desired_from_spec,
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


@pytest.mark.parametrize(
    ("description", "observed", "expected"),
    [
        ("no session key yet -> create", None, Create),
        ("session key present + live, cap matches -> noop", _eph_state(expires_at=_FUTURE), NoOp),
        ("session cap drift -> update", _eph_state(limit=1.0, expires_at=_FUTURE), Update),
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
