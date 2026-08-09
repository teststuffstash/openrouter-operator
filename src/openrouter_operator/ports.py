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


class RateLimited(Exception):
    """A 429 from the OpenRouter key API, raised by the port (issue #26).

    The SDK's own exception classes stay behind `adapter.py` — this typed error is how a 429
    crosses the port, so the pure `reconcile.decide_retry` can classify it (an rpd-class limit
    parks until the UTC reset; anything else backs off normally) without importing the SDK.

    `limit_name` is the limit the API named (e.g. `keys-modify-api-rpd-v2`), falling back to the
    raw error text when it named none — classification reads it as free text either way, so a
    payload shape change degrades to a normal backoff rather than a mis-park.
    """

    def __init__(self, limit_name: str | None = None) -> None:
        super().__init__(limit_name or "rate limited")
        self.limit_name = limit_name


@dataclass(frozen=True)
class KeyState:
    """Observed state of a runtime key on OpenRouter.

    `reset_interval` is `None` for a key minted with no reset window (an ephemeral session key) —
    do NOT default it to weekly, or it perpetually reads as drifted against a no-reset desired.

    `expires_at` / `disabled` let the reconciler tell a LIVE key from a dead one: OpenRouter still
    returns an expired/revoked key's record (so a stale `status.hash` looks "minted"), but using it
    yields `401 User not found`. decide() re-mints a dead key instead of NoOp'ing on the corpse.

    `usage` is the dollars this key has already spent, and `None` means the read did not report it
    (absent field / shape change) — NOT zero. Proactive age renewal (issue #25) carries the
    remaining budget onto the fresh key, so an unknown spend must stay distinguishable from no
    spend: renewing on a `0.0` we invented would hand back a cap the session had already eaten.
    """

    hash: str
    name: str
    limit: float
    reset_interval: ResetInterval | None
    expires_at: datetime | None = None
    disabled: bool = False
    usage: float | None = None


@dataclass(frozen=True)
class MintedKey:
    """Result of creating a key — the secret value is returned exactly once, here."""

    hash: str
    value: str


@dataclass(frozen=True)
class AccountCredits:
    """The account-scope credit ledger from `GET /api/v1/credits` (issue #29).

    Account-wide, not per-key: this is the pay-as-you-go pot every minted key spends out of, and
    the second exhaustion of the #26 incident (the balance was at $0.17 and nothing watched it).
    """

    total_credits: float
    total_usage: float

    @property
    def balance_usd(self) -> float:
        """What is left to spend. Negative is possible (usage can pass the grant) and is NOT
        clamped: a clamped 0 is exactly the reading the gauge must stay able to tell apart."""
        return self.total_credits - self.total_usage


class AccountPort(Protocol):
    """Account-scope reads, deliberately a SEPARATE port from `OpenRouterPort`.

    A credits read spends no `keys-modify` budget, so it is not something `MeteredPort` should
    count, and keeping it off the key port means no existing key fake grows a method it has no
    opinion about. One adapter implements both.
    """

    def get_account_credits(self) -> AccountCredits: ...


class OpenRouterPort(Protocol):
    """The operations the operator needs from OpenRouter. The adapter implements this.

    Every method may raise `RateLimited` when OpenRouter answers 429; the key-MODIFY ops
    (create/update/delete) are the ones metered by the daily `keys-modify-api-rpd-*` budget.
    """

    def get_key(self, key_hash: str) -> KeyState | None: ...

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey: ...

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None: ...

    def delete_key(self, key_hash: str) -> None:
        """Ensure the key is gone. Succeeds when it is ALREADY absent upstream (issue #30) —
        the post-condition is "no such key", not "this call did the deleting", so a caller can
        clear a finalizer on return. Other failures still raise."""
        ...
