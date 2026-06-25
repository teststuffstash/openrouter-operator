"""The narrow port (interface) the reconcile logic depends on.

This is the whole point of the SDK-over-provider-http argument: the reconcile logic talks to this
small, fully-typed Protocol — never the SDK directly — so it's trivially testable with a fake, and
the real SDK lives behind a single adapter. Mock the port, not the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import ResetInterval


@dataclass(frozen=True)
class KeyState:
    """Observed state of a runtime key on OpenRouter."""

    hash: str
    name: str
    limit: float
    reset_interval: ResetInterval


@dataclass(frozen=True)
class MintedKey:
    """Result of creating a key — the secret value is returned exactly once, here."""

    hash: str
    value: str


class OpenRouterPort(Protocol):
    """The operations the operator needs from OpenRouter. The adapter implements this."""

    def get_key(self, key_hash: str) -> KeyState | None: ...

    def create_key(self, name: str, limit: float, reset: ResetInterval) -> MintedKey: ...

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval) -> None: ...

    def delete_key(self, key_hash: str) -> None: ...
