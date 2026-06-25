"""Adapter: implements OpenRouterPort via the official `openrouter` Python SDK (beta).

This is the *only* place the SDK is touched. The SDK is auto-generated from OpenRouter's OpenAPI
spec, so a breaking API change shows up as a type/attribute error here when the SDK is bumped —
caught by mypy/CI, not at reconcile time in the cluster.

NOTE (beta): the SDK's exact key-management method names/fields are not pinned yet. The calls below
are best-effort against the documented surface (`api_keys.create/list/get/update/delete`, with
`limit` + `limit_reset`). Verify against the installed SDK and tighten — at which point you can drop
the `openrouter.*` mypy override in pyproject and get full compile-time checking of this file too.
"""

from __future__ import annotations

from typing import Any

from .models import ResetInterval
from .ports import KeyState, MintedKey


class OpenRouterAdapter:
    def __init__(self, management_key: str) -> None:
        from openrouter import (
            OpenRouter,
        )  # lazy: only needed at runtime (optional `sdk` extra)

        self._client = OpenRouter(api_key=management_key)

    @property
    def _keys(self) -> Any:
        return self._client.api_keys

    def get_key(self, key_hash: str) -> KeyState | None:
        resp = self._keys.get(key_hash)
        data = getattr(resp, "data", resp)
        if data is None:
            return None
        return _to_state(data)

    def create_key(
        self, name: str, limit: float, reset: ResetInterval, guardrail: str | None
    ) -> MintedKey:
        resp = self._keys.create(
            name=name, limit=limit, limit_reset=reset.value, guardrail=guardrail
        )
        data = getattr(resp, "data", resp)
        return MintedKey(hash=str(data.hash), value=str(data.key))

    def update_key(
        self, key_hash: str, limit: float, reset: ResetInterval, guardrail: str | None
    ) -> None:
        self._keys.update(key_hash, limit=limit, limit_reset=reset.value, guardrail=guardrail)

    def delete_key(self, key_hash: str) -> None:
        self._keys.delete(key_hash)


def _to_state(data: Any) -> KeyState:
    reset_raw = getattr(data, "limit_reset", None) or ResetInterval.weekly.value
    return KeyState(
        hash=str(data.hash),
        name=str(getattr(data, "name", "")),
        limit=float(getattr(data, "limit", 0.0) or 0.0),
        reset_interval=ResetInterval(reset_raw),
        guardrail=getattr(data, "guardrail", None),
    )
