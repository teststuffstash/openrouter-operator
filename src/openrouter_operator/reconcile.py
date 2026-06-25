"""The pure reconcile decision — no I/O, no SDK. This is the part under the decision-table test.

`decide(desired, observed) -> Plan` is a total function over (desired spec x observed key state).
The kopf handler turns a Plan into port calls; this module just decides *what* should happen.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import OpenRouterKeySpec, ResetInterval
from .ports import KeyState


@dataclass(frozen=True)
class Desired:
    """The key we want to exist, derived from a spec."""

    name: str
    limit: float
    reset_interval: ResetInterval
    guardrail: str | None


@dataclass(frozen=True)
class Create:
    desired: Desired


@dataclass(frozen=True)
class Update:
    key_hash: str
    desired: Desired


@dataclass(frozen=True)
class NoOp:
    pass


Plan = Create | Update | NoOp


def desired_from_spec(spec: OpenRouterKeySpec) -> Desired:
    return Desired(
        name=spec.key_name(),
        limit=spec.budget_usd,
        reset_interval=spec.reset_interval,
        guardrail=spec.guardrail,
    )


def decide(desired: Desired, observed: KeyState | None) -> Plan:
    """Decide the action to reconcile `observed` toward `desired`.

    - no key yet            -> Create
    - budget/reset/guardrail drifted -> Update
    - already correct        -> NoOp
    """
    if observed is None:
        return Create(desired)

    drifted = (
        observed.limit != desired.limit
        or observed.reset_interval != desired.reset_interval
        or observed.guardrail != desired.guardrail
    )
    if drifted:
        return Update(observed.hash, desired)

    return NoOp()
