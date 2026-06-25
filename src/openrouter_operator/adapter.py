"""Adapter: implements OpenRouterPort via the official `openrouter` Python SDK (0.10.x).

Verified against the live SDK: `api_keys.{create,get,update,delete}` are keyword-only; create's
response carries `.key` (the secret value, returned once) and `.data` (the key record, incl.
`.hash`). Guardrails are NOT attached at key creation — that's a separate OpenRouter guardrails
assign step (not yet wired; the CRD's `guardrail` field is currently inert).
"""

from __future__ import annotations

from typing import Any

from .models import ResetInterval
from .ports import KeyState, MintedKey


class OpenRouterAdapter:
    def __init__(self, management_key: str) -> None:
        from openrouter import OpenRouter

        self._client = OpenRouter(api_key=management_key)

    @property
    def _keys(self) -> Any:
        return self._client.api_keys

    def get_key(self, key_hash: str) -> KeyState | None:
        resp = self._keys.get(hash=key_hash)
        data = getattr(resp, "data", None)
        return _to_state(data) if data is not None else None

    def create_key(self, name: str, limit: float, reset: ResetInterval) -> MintedKey:
        resp = self._keys.create(name=name, limit=limit, limit_reset=reset.value)
        return MintedKey(hash=str(resp.data.hash), value=str(resp.key))

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval) -> None:
        self._keys.update(hash=key_hash, limit=limit, limit_reset=reset.value)

    def delete_key(self, key_hash: str) -> None:
        self._keys.delete(hash=key_hash)


def _to_state(data: Any) -> KeyState:
    reset_raw = getattr(data, "limit_reset", None) or ResetInterval.weekly.value
    return KeyState(
        hash=str(data.hash),
        name=str(getattr(data, "name", "")),
        limit=float(getattr(data, "limit", 0.0) or 0.0),
        reset_interval=ResetInterval(reset_raw),
    )
