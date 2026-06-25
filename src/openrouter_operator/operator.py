"""kopf handlers — thin glue: parse spec -> observe -> decide() -> apply via the port.

All the judgement lives in reconcile.decide() (pure, tested). This module only does I/O.
"""

from __future__ import annotations

import os
from typing import Any

import kopf

from .adapter import OpenRouterAdapter
from .k8s import write_key_secret
from .models import OpenRouterKeySpec
from .ports import OpenRouterPort
from .reconcile import Create, NoOp, Update, decide, desired_from_spec

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
    plan = decide(desired, observed)

    if isinstance(plan, Create):
        minted = port.create_key(
            desired.name, desired.limit, desired.reset_interval, desired.guardrail
        )
        write_key_secret(namespace, parsed.target_secret_name(), minted.value)
        patch.status["openrouter"] = {"hash": minted.hash}
    elif isinstance(plan, Update):
        port.update_key(plan.key_hash, desired.limit, desired.reset_interval, desired.guardrail)
    elif isinstance(plan, NoOp):
        pass


@kopf.on.delete(GROUP, VERSION, PLURAL)
def delete_key(*, status: kopf.Status, **_: Any) -> None:
    key_hash = (status.get("openrouter") or {}).get("hash")
    if key_hash:
        _port().delete_key(key_hash)
