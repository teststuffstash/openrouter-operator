"""The pure reconcile decision — no I/O, no SDK. This is the part under the decision-table test.

`decide(desired, observed) -> Plan` is a total function over (desired spec x observed key state).
The kopf handler turns a Plan into port calls; this module just decides *what* should happen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from .models import OpenRouterKeySpec, ResetInterval
from .ports import KeyState, SecretState


@dataclass(frozen=True)
class Desired:
    """The key we want to exist, derived from a spec.

    `reset_interval` is `None` for an ephemeral session key (no reset window — a one-shot hard cap).
    `expires_at` is set-once at mint time (not reconciled), so the key self-destructs server-side.
    """

    name: str
    limit: float
    reset_interval: ResetInterval | None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Create:
    desired: Desired


@dataclass(frozen=True)
class Update:
    key_hash: str
    desired: Desired


@dataclass(frozen=True)
class Rotate:
    """Create a fresh key, swap the Secret, THEN delete the old key — for drift a PATCH cannot fix.

    OpenRouter's key PATCH cannot change `expires_at` (issue #6, proven live 2026-07-09): a
    re-applied CR with a later `spec.expiresAt` reconciled "successfully" while the real key kept
    its original deadline — and a healthy agent run died at that stale deadline mid-run. Expiry
    drift therefore rotates. Order matters in the executor: mint + Secret swap BEFORE deleting the
    old key, so a consumer never observes a window with no live credential.

    `delete_old` is False for a proactive AGE renewal (issue #25): that rotation fires precisely
    because the old key is minutes from lapsing on its own, and deleting it early would 401 any
    consumer still resolving a ≤60s-stale cached reference to it. Drift rotation keeps deleting.
    """

    key_hash: str
    desired: Desired
    delete_old: bool = True


@dataclass(frozen=True)
class NoOp:
    pass


@dataclass(frozen=True)
class NormalizeSecret:
    """The upstream key is healthy, but the k8s Secret is unlabeled or shape-drifted.

    The operator should read the existing Secret value and rewrite it with correct labels and
    data keys (OPENROUTER_API_KEY, KEY_HASH, GUARDRAIL) via `write_key_secret()`. Idempotent:
    the key value itself is only known at mint, so a wrong-VALUE Secret still needs a Rotate.
    """

    pass


@dataclass(frozen=True)
class SecretMissing:
    """The upstream key is healthy, but the k8s Secret is absent (deleted out of band).

    The key value is only returned once at mint time, so we cannot recover it without minting
    a fresh key — and re-minting would delete a live, healthy fleet credential (the old key is
    deleted on Rotate by default). The operator surfaces this as a status condition on the CR
    and a Prometheus metric; a human re-mints by deleting and re-applying the CR.
    """

    secret_name: str


Plan = Create | Update | Rotate | NoOp | NormalizeSecret | SecretMissing

# OpenRouter stores a requested expiry rounded by a few seconds (a key minted for 18:40:10 reads
# back 18:40:08) — strict equality would rotate every reconcile, forever. Anything inside this
# window is "the same instant"; real drift (a re-mint extending a session) is minutes-to-hours.
EXPIRY_TOLERANCE_S = 120.0

# ── Proactive age renewal of a session key (issue #25) ──────────────────────────────────────────
# How close to a live key's own expiry the operator re-mints. Two floors set it: it must be at
# least 2x the reconcile timer (900s) so a pass cannot be missed and land on a dead key, and it
# must be comfortably longer than the credential rail's cached-ref TTL (60s) so the swapped Secret
# has resolved everywhere before the old key lapses.
RENEW_THRESHOLD_S = 1800.0

# The window a renewal mints. Much longer than RENEW_THRESHOLD_S, so a renewal clears its own
# trigger for hours rather than re-firing on the next pass; short enough that a key still
# self-destructs promptly if the operator dies — and well inside GC_GRACE, so the expiry sweep
# still collects the CR and its Secret on the spec's own schedule.
RENEW_WINDOW = timedelta(hours=2)


def desired_from_spec(spec: OpenRouterKeySpec) -> Desired:
    # NB: spec.guardrail is intentionally not used yet — guardrails attach via a separate
    # OpenRouter assign step, not at key create/update, so reconciling on it would loop forever.
    return Desired(
        name=spec.key_name(),
        limit=spec.budget_usd,
        reset_interval=spec.effective_reset(),  # None for an ephemeral session key
        expires_at=spec.expires_at,
    )


def decide(
    desired: Desired,
    observed: KeyState | None,
    secret: SecretState | None,
    now: datetime,
    *,
    secret_name: str,
) -> Plan:
    """Decide the action to reconcile `observed` toward `desired`.

    - no key yet, or the existing one is DEAD (expired/revoked) -> Create (mint / re-mint)
    - inside the renewal window (issue #25)                      -> renew on age, or leave alone
    - expiry drifted (PATCH can't fix that — issue #6)           -> Rotate (mint+swap+delete)
    - budget/reset drifted                                       -> Update
    - already correct                                            -> NoOp

    Self-heal: OpenRouter keeps returning an expired/revoked key's record, so a stale `status.hash`
    looks minted while the key 401s. Treat a dead key like an absent one and re-mint. But only when
    the result would actually be LIVE — if the spec's own `expires_at` is already past, a re-mint
    (or rotate) would be born-dead and hot-loop, so NoOp and wait for a fresh CR instead.
    """
    if observed is None or _is_dead(observed, now):
        if desired.expires_at is None or desired.expires_at > now:
            return Create(desired)
        return NoOp()

    if _in_renewal_window(desired, now):
        return _renewal_plan(desired, observed, now)

    if _expiry_drifted(desired, observed):
        # No born-dead guard needed here any more: drift is only ever reported for a spec that
        # WANTS an expiry, and the renewal window above already returned for every such spec whose
        # deadline is within RENEW_THRESHOLD_S — let alone past. So this rotation is always live.
        return Rotate(observed.hash, desired)

    drifted = observed.limit != desired.limit or observed.reset_interval != desired.reset_interval
    if drifted:
        return Update(observed.hash, desired)

    # Upstream key is healthy — check the k8s Secret state (issue #53).
    # A legacy/adopted Secret that is missing, unlabeled, or shape-drifted must be repaired
    # on an ordinary NoOp pass, not left to silently break credential resolution.
    if secret is None or not secret.exists:
        # Secret is missing entirely — we cannot recover the key value (it is only returned
        # once at mint time), and re-minting would delete a live, healthy fleet credential
        # (the old key is deleted on Rotate by default). Leave the upstream key alone and
        # surface the gap as a status condition + metric (issue #56).
        return SecretMissing(secret_name=secret_name)
    if not secret.has_label or not secret.has_all_keys:
        # Secret exists but is unlabeled or shape-drifted. The value key is unrecoverable
        # (only known at mint time), so distinguish it from metadata-key drift (issue #58).
        if not secret.has_openrouter_key:
            return SecretMissing(secret_name=secret_name)
        # Secret exists but is missing only metadata keys or the label — read the existing
        # value and rewrite it with correct labels and data keys.
        return NormalizeSecret()

    return NoOp()


def _in_renewal_window(desired: Desired, now: datetime) -> bool:
    """Has the clock reached the point where THIS operator owns the key's lifetime (issue #25)?

    True from `RENEW_THRESHOLD_S` before the spec's deadline onward — and it stays true once that
    deadline passes, because a renewed key deliberately outlives it. Inside the window the spec's
    `expiresAt` no longer describes the live key, so it is neither a drift reference (comparing
    against it would rotate-loop the fresh window we just minted) nor a cap reference (an Update
    would PATCH the carried remainder back up to the full `budgetUSD`). Only the age decision runs.

    A standing project key has no deadline and is never in the window: it is the funding ceiling,
    not a session breaker, so nothing about it is age-triggered.
    """
    if desired.expires_at is None:
        return False
    return (desired.expires_at - now).total_seconds() <= RENEW_THRESHOLD_S


def _renewal_plan(desired: Desired, observed: KeyState, now: datetime) -> Plan:
    """Rotate a live session key before it dies of AGE — or deliberately do nothing (issue #25).

    A ride that outlasts its key's honest window 401-storms to death (sleep-tracking#96), so once
    the live key is within `RENEW_THRESHOLD_S` of ITS OWN expiry, mint a replacement and swap the
    Secret; the credential rail resolves the new value per request, so a running ride picks it up
    without a restart. The fresh `RENEW_WINDOW` is what clears the trigger — re-minting the same
    instant would fire again on every pass.

    The cap is what makes this a fix rather than a breaker regression: the replacement carries the
    tighter of the live and desired caps MINUS what has already been spent, so spend accumulates
    down the whole rotation chain and total spend across old+new keys never exceeds the one
    `budgetUSD` the CR asked for. Both edges of that are fail-safe, and both mean "do nothing":

    * spend the read did not report — never mint a fresh cap on unknown spend; the key then dies
      of age exactly as it does today, which is strictly better than silently refunding a session;
    * nothing left to carry — an exhausted key 403s BY DESIGN (`_is_dead` leaves it alone for the
      same reason), and age renewal must not resurrect it.
    """
    if observed.expires_at is None:
        return NoOp()  # a key with no deadline of its own cannot die of age
    if (observed.expires_at - now).total_seconds() > RENEW_THRESHOLD_S:
        return NoOp()  # still comfortably live — the trigger is age, not every pass
    if observed.usage is None:
        return NoOp()
    remaining = min(observed.limit, desired.limit) - observed.usage
    if remaining <= 0:
        return NoOp()
    renewed = replace(desired, limit=remaining, expires_at=now + RENEW_WINDOW)
    return Rotate(observed.hash, renewed, delete_old=False)


def _expiry_drifted(desired: Desired, observed: KeyState) -> bool:
    """Expiry drift only a Rotate can reconcile (issue #6: PATCH silently keeps the old deadline).
    Only meaningful when the spec WANTS an expiry (session keys): a standing key (expires_at None)
    ignores whatever the live key carries. The tolerance absorbs OpenRouter's storage rounding so a
    matched key never rotate-loops."""
    if desired.expires_at is None:
        return False
    if observed.expires_at is None:
        return True
    return abs((observed.expires_at - desired.expires_at).total_seconds()) > EXPIRY_TOLERANCE_S


def _is_dead(observed: KeyState, now: datetime) -> bool:
    """A key OpenRouter will reject: revoked, or past its expiry. NOT budget-exhausted — re-minting
    an exhausted key would hand out a fresh `budgetUSD`, defeating the per-session cap, so an
    exhausted key is intentionally left to 403."""
    if observed.disabled:
        return True
    return observed.expires_at is not None and observed.expires_at <= now


# ── Retry classification for a 429 on the key API (issue #26) ───────────────────────────────────


@dataclass(frozen=True)
class ParkUntilReset:
    """Hold the op until the daily budget rolls over — retrying before then CANNOT succeed."""

    delay_s: float


@dataclass(frozen=True)
class Backoff:
    """Not provably a daily limit: let the caller's normal exponential backoff handle it."""


RetryPlan = ParkUntilReset | Backoff

# Substrings that mark a requests-per-DAY budget. The incident limit is `keys-modify-api-rpd-v2`;
# match on markers rather than that exact id so a `-v3` (or a spelled-out message) still parks.
DAILY_LIMIT_MARKERS = ("rpd", "per-day", "per_day", "requests-per-day", "daily")

# Floor on the park. A 429 landing seconds before midnight would otherwise compute a ~0s delay and
# hot-spin across the boundary — exactly the hammer this fix removes.
MIN_PARK_S = 60.0


def decide_retry(limit_name: str | None, now: datetime) -> RetryPlan:
    """Decide how to retry a 429 from the key API, given the limit the port reported.

    OpenRouter's `keys-modify-api-rpd-v2` is calendar-bound: it resets at UTC midnight and nothing
    else clears it (proven live on 2026-08-08 — a top-up from $0.17 to $20.17 left the 429s at an
    unchanged rate, so there is deliberately no balance-recovery re-probe here). Parking until the
    reset is therefore the only retry that can succeed, and a hot retry before it is pure hammer.

    Anything NOT provably daily backs off normally: a per-minute burst limit clears in seconds, so
    parking it for hours would stall reconcile far worse than the limit itself, and an unnamed 429
    could be either. Both directions of that trade-off are pinned in the decision table.
    """
    if limit_name is None:
        return Backoff()
    haystack = limit_name.lower()
    if not any(marker in haystack for marker in DAILY_LIMIT_MARKERS):
        return Backoff()
    return ParkUntilReset(max(seconds_until_daily_reset(now), MIN_PARK_S))


def seconds_until_daily_reset(now: datetime) -> float:
    """Seconds from `now` (any aware clock) until the next UTC midnight, when the rpd budget rolls
    over. Normalised to UTC first: the reset is the API's calendar day, not the reporter's."""
    now_utc = now.astimezone(UTC)
    next_reset = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (next_reset - now_utc).total_seconds()


# Grace window before an expired ephemeral CR is garbage-collected (issue #10). The key self-
# destructs server-side at expiresAt, but the CR + its Secret linger forever — nothing generates an
# event when the clock passes expiresAt, so a periodic timer must sweep them. 24h of grace lets a
# slow finalize/ledger-reflex read the key's KEY_HASH before it's gone.
GC_GRACE = timedelta(hours=24)


def should_collect(spec: OpenRouterKeySpec, now: datetime) -> bool:
    """GC predicate (issue #10): should this CR be collected by the expiry timer?

    Collect an EPHEMERAL session key whose `expiresAt` passed more than `GC_GRACE` ago — the key is
    already dead server-side, so the CR + its Secret are pure litter. A STANDING key is NEVER
    collected: it's the funding ceiling, and its reset is a soft per-window cap, not a
    self-destruct. An ephemeral key with no `expiresAt` is also kept (no deadline to sweep on).
    """
    if not spec.ephemeral:
        return False
    if spec.expires_at is None:
        return False
    return now > spec.expires_at + GC_GRACE
