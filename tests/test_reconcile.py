"""Decision-table tests for the pure reconcile logic — the homelab testing doctrine: one
parametrized table of (observed key x desired spec) -> expected Plan, reviewable at a glance,
offline, no SDK. A reviewer can see a missing case in the table.
"""

from __future__ import annotations

import pytest

from openrouter_operator.models import OpenRouterKeySpec, ResetInterval
from openrouter_operator.ports import KeyState, MintedKey, OpenRouterPort
from openrouter_operator.reconcile import (
    Create,
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


def _state(
    *,
    limit: float = 5.0,
    reset: ResetInterval = ResetInterval.weekly,
    guardrail: str | None = "only-free",
) -> KeyState:
    return KeyState(
        hash="GK1",
        name="sleep-tracking-agent",
        limit=limit,
        reset_interval=reset,
        guardrail=guardrail,
    )


@pytest.mark.parametrize(
    ("description", "observed", "expected"),
    [
        ("no key yet -> create", None, Create),
        ("everything matches -> noop", _state(), NoOp),
        ("budget drift -> update", _state(limit=10.0), Update),
        ("reset drift -> update", _state(reset=ResetInterval.monthly), Update),
        ("guardrail changed -> update", _state(guardrail="no-opus"), Update),
        ("guardrail removed -> update", _state(guardrail=None), Update),
    ],
)
def test_decide(description: str, observed: KeyState | None, expected: type[Plan]) -> None:
    plan = decide(DESIRED, observed)
    assert isinstance(plan, expected), description
    if isinstance(plan, Update):
        assert observed is not None
        assert plan.key_hash == observed.hash
        assert plan.desired == DESIRED


def test_desired_from_spec_maps_fields() -> None:
    assert DESIRED.name == "sleep-tracking-agent"
    assert DESIRED.limit == 5.0
    assert DESIRED.reset_interval is ResetInterval.weekly
    assert DESIRED.guardrail == "only-free"


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


class _FakePort:
    """A fake OpenRouterPort — proves the Protocol is satisfiable and is the testing seam for
    handler-level tests later (mock the port, never the live API)."""

    def get_key(self, key_hash: str) -> KeyState | None:
        return None

    def create_key(
        self, name: str, limit: float, reset: ResetInterval, guardrail: str | None
    ) -> MintedKey:
        return MintedKey(hash="GKnew", value="sk-or-v1-fake")

    def update_key(
        self, key_hash: str, limit: float, reset: ResetInterval, guardrail: str | None
    ) -> None:
        return None

    def delete_key(self, key_hash: str) -> None:
        return None


def test_fake_port_satisfies_protocol() -> None:
    port: OpenRouterPort = _FakePort()
    assert port.get_key("x") is None
    minted = port.create_key("demo-agent", 1.0, ResetInterval.weekly, None)
    assert minted.value.startswith("sk-or-")
