"""The narrow port (interface) the reconcile logic depends on.

This is the whole point of the SDK-over-provider-http argument: the reconcile logic talks to this
small, fully-typed Protocol — never the SDK directly — so it's trivially testable with a fake, and
the real SDK lives behind a single adapter. Mock the port, not the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .models import ResetInterval


@dataclass(frozen=True)
class KeyState:
    """Observed state of a runtime key on OpenRouter.

    `reset_interval` is `None` for a key minted with no reset window (an ephemeral session key) —
    do NOT default it to weekly, or it perpetually reads as drifted against a no-reset desired.

    `expires_at` / `disabled` let the reconciler tell a LIVE key from a dead one: OpenRouter still
    returns an expired/revoked key's record (so a stale `status.hash` looks "minted"), but using it
    yields `401 User not found`. decide() re-mints a dead key instead of NoOp'ing on the corpse.
    """

    hash: str
    name: str
    limit: float
    reset_interval: ResetInterval | None
    expires_at: datetime | None = None
    disabled: bool = False


@dataclass(frozen=True)
class MintedKey:
    """Result of creating a key — the secret value is returned exactly once, here."""

    hash: str
    value: str


class OpenRouterPort(Protocol):
    """The operations the operator needs from OpenRouter. The adapter implements this."""

    def get_key(self, key_hash: str) -> KeyState | None: ...

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey: ...

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None: ...

    def delete_key(self, key_hash: str) -> None: ...
