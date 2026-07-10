"""kopf handlers — thin glue: parse spec -> observe -> decide() -> apply via the port.

All the judgement lives in reconcile.decide() (pure, tested). This module only does I/O.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import kopf

from .adapter import OpenRouterAdapter
from .k8s import write_key_secret
from .models import OpenRouterKeySpec
from .ports import KeyState, OpenRouterPort
from .reconcile import Create, NoOp, Rotate, Update, decide, desired_from_spec

GROUP = "openrouter.teststuff.net"
VERSION = "v1alpha1"
PLURAL = "openrouterkeys"


def _port() -> OpenRouterPort:
    return OpenRouterAdapter(os.environ["OPENROUTER_MANAGEMENT_KEY"])


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile_key(
    *,
    spec: kopf.Spec,
    status: kopf.Status,
    namespace: str | None,
    patch: kopf.Patch,
    **_: Any,
) -> None:
    parsed = OpenRouterKeySpec.model_validate(dict(spec))
    desired = desired_from_spec(parsed)
    if namespace is None:  # the CRD is Namespaced; satisfy the type + guard regardless
        raise kopf.PermanentError("OpenRouterKey must be namespaced")
    port = _port()

    key_hash = (status.get("openrouter") or {}).get("hash")
    observed = port.get_key(key_hash) if key_hash else None
    plan = decide(desired, observed, datetime.now(UTC))

    if isinstance(plan, Create):
        minted = port.create_key(
            desired.name, desired.limit, desired.reset_interval, desired.expires_at
        )
        write_key_secret(
            namespace,
            parsed.target_secret_name(),
            minted.value,
            minted.hash,
            parsed.guardrail or "",
        )
        patch.status["openrouter"] = _key_status(port, minted.hash)
    elif isinstance(plan, Rotate):
        # Expiry drift PATCH can't fix (issue #6): mint fresh + swap the Secret FIRST, delete the
        # old key LAST — a consumer never observes a window without a live credential.
        minted = port.create_key(
            desired.name, desired.limit, desired.reset_interval, desired.expires_at
        )
        write_key_secret(
            namespace,
            parsed.target_secret_name(),
            minted.value,
            minted.hash,
            parsed.guardrail or "",
        )
        patch.status["openrouter"] = _key_status(port, minted.hash)
        port.delete_key(plan.key_hash)
    elif isinstance(plan, Update):
        port.update_key(plan.key_hash, desired.limit, desired.reset_interval)
        patch.status["openrouter"] = _key_status(port, plan.key_hash)
    elif isinstance(plan, NoOp) and observed is not None:
        # Surface the LIVE expiry even on a no-change pass, so dispatch-time pre-flights
        # (homelab agent-session.sh: refuse a key with <30 min real life) read truth, not the spec.
        patch.status["openrouter"] = _observed_status(observed)


def _key_status(port: OpenRouterPort, key_hash: str) -> dict[str, Any]:
    """Status block for a key we just touched: re-read it so `expires_at` is the value OpenRouter
    actually STORED (it rounds the requested instant by seconds) — the spec's wish is not status."""
    state = port.get_key(key_hash)
    if state is None:  # read-back raced/failed; the hash alone still lets the next pass reconcile
        return {"hash": key_hash}
    return _observed_status(state)


def _observed_status(state: KeyState) -> dict[str, Any]:
    return {
        "hash": state.hash,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
    }


@kopf.on.delete(GROUP, VERSION, PLURAL)
def delete_key(*, status: kopf.Status, **_: Any) -> None:
    key_hash = (status.get("openrouter") or {}).get("hash")
    if key_hash:
        _port().delete_key(key_hash)
