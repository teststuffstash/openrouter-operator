"""Adapter: implements OpenRouterPort via the official `openrouter` Python SDK (0.10.x).

Verified against the live SDK (0.10.0): `api_keys.{create,get,update,delete}` are keyword-only;
create's response carries `.key` (the secret value, returned once) and `.data` (the key record,
incl. `.hash`). `create`/`update` take `limit_reset: OptionalNullable[...]` and `create` takes
`expires_at: OptionalNullable[datetime]` — passing `None` serialises to JSON `null` (= no reset
window / no expiry), which is exactly how an ephemeral session key is minted (hard one-shot cap).
Guardrails are NOT attached at key creation — that's a separate OpenRouter guardrails assign step
(not yet wired; the CRD's `guardrail` field is currently inert).

Account credit (issue #29) also comes from this SDK — no raw HTTP and no proxy hop needed.
Verified against 0.10.0, the version `uv.lock` pins and the image installs `--frozen`:
`client.credits.get_credits()` exists, takes only optional keyword args, and returns
`GetCreditsResponse.data` with `total_credits: float` / `total_usage: float` — the same two fields
as the live `GET /api/v1/credits` 200 recorded on the issue.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .models import ResetInterval
from .ports import AccountCredits, KeyState, MintedKey, RateLimited


class OpenRouterAdapter:
    def __init__(self, management_key: str) -> None:
        from openrouter import OpenRouter

        self._client = OpenRouter(api_key=management_key)

    @property
    def _keys(self) -> Any:
        return self._client.api_keys

    def get_account_credits(self) -> AccountCredits:
        """Account-scope ledger — the same call the fleet's dispatch pre-flight makes, but with
        this operator's provisioning key (accepted: HTTP 200, live probe on issue #29)."""
        data = _call(self._client.credits.get_credits).data
        return AccountCredits(
            total_credits=float(data.total_credits), total_usage=float(data.total_usage)
        )

    def get_key(self, key_hash: str) -> KeyState | None:
        resp = _call(self._keys.get, hash=key_hash)
        data = getattr(resp, "data", None)
        return _to_state(data) if data is not None else None

    def create_key(
        self,
        name: str,
        limit: float,
        reset: ResetInterval | None,
        expires_at: datetime | None = None,
    ) -> MintedKey:
        resp = _call(
            self._keys.create,
            name=name,
            limit=limit,
            limit_reset=_reset_value(reset),  # None -> null -> no reset window (session key)
            expires_at=expires_at,  # None -> null -> no expiry
        )
        return MintedKey(hash=str(resp.data.hash), value=str(resp.key))

    def update_key(self, key_hash: str, limit: float, reset: ResetInterval | None) -> None:
        _call(self._keys.update, hash=key_hash, limit=limit, limit_reset=_reset_value(reset))

    def delete_key(self, key_hash: str) -> None:
        """Delete the runtime key. Idempotent: a key already absent upstream is SUCCESS (#30).

        The desired state — no such key on OpenRouter — already holds, so a not-found must not
        propagate: kopf retried it forever and the CR's finalizer never cleared (11 wedged CRs at
        filing, on keys deleted mid-storm or self-destructed at expiry). Scoped to DELETE on
        purpose — a 404 on get/update means the key vanished under us mid-reconcile, and that has
        to keep surfacing.
        """
        try:
            _call(self._keys.delete, hash=key_hash)
        except Exception as exc:
            if not _is_not_found(exc):
                raise


def _call(op: Callable[..., Any], **kwargs: Any) -> Any:
    """Run an SDK call, translating a 429 into the port's typed `RateLimited` (issue #26).

    This is the only place that touches SDK exception shapes; the retry *decision* is pure and
    lives in `reconcile.decide_retry`. Duck-typed on purpose — the beta SDK's error classes move,
    and anything we fail to recognise stays the original exception (normal backoff), never a
    silently swallowed error.
    """
    try:
        return op(**kwargs)
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status != 429:
            raise
        raise RateLimited(_limit_name(exc)) from exc


def _is_not_found(exc: BaseException) -> bool:
    """Does this SDK error mean "no such key upstream"? (issue #30)

    Duck-typed for the same reason `_call` is — the beta SDK's error classes move. The incident
    error (`openrouter.errors.notfoundresponse_error.NotFoundResponseError: API key not found`)
    reached the log with no HTTP status attached, so match the class NAME as well as the status:
    either signal alone is enough, and a rename to some other `*NotFound*` still reads as absent.
    Anything unrecognised stays an exception (normal kopf backoff), never a silent success.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 404:
        return True
    return "notfound" in type(exc).__name__.lower()


def _limit_name(exc: Exception) -> str:
    """Best-effort limit identifier for a 429: the name the API gave, else the raw error text.
    `decide_retry` matches markers as free text, so the fallback still classifies correctly."""
    for source in (getattr(exc, "body", None), getattr(exc, "data", None)):
        if isinstance(source, dict):
            error = source.get("error")
            metadata = error.get("metadata") if isinstance(error, dict) else None
            named = metadata.get("limit_name") if isinstance(metadata, dict) else None
            if named:
                return str(named)
    return str(exc)


def _reset_value(reset: ResetInterval | None) -> str | None:
    return reset.value if reset is not None else None


def _to_state(data: Any) -> KeyState:
    # Preserve a null/absent limit_reset as None — a no-reset (session) key must not read as weekly,
    # or decide() sees perpetual drift and update-loops it forever.
    reset_raw = getattr(data, "limit_reset", None)
    return KeyState(
        hash=str(data.hash),
        name=str(getattr(data, "name", "")),
        limit=float(getattr(data, "limit", 0.0) or 0.0),
        reset_interval=ResetInterval(reset_raw) if reset_raw else None,
        expires_at=_parse_dt(getattr(data, "expires_at", None)),
        disabled=bool(getattr(data, "disabled", False)),
        usage=_opt_float(getattr(data, "usage", None)),
    )


def _opt_float(value: Any) -> float | None:
    """Spend as a float, or None when the record did not report it (issue #25). Deliberately NOT
    defaulted to 0.0: a renewal that reads "no spend" mints the full cap again, so an absent or
    unparseable `usage` must stay unknown and make the renewal skip the pass instead."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    """SDK hands back a datetime or an ISO-8601 string; normalise to an aware datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
