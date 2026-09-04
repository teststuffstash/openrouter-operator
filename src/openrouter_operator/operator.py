"""kopf handlers — thin glue: parse spec -> observe -> decide() -> apply via the port.

All the judgement lives in reconcile.decide() (pure, tested). This module only does I/O.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ParamSpec, TypeVar

import kopf

from .adapter import OpenRouterAdapter
from .k8s import (
    REQUIRED_DATA_KEYS,
    SESSION_KEY_LABEL,
    delete_cr,
    delete_key_secret,
    read_key_secret,
    write_key_secret,
)
from .metrics import KeyOpMetrics, MeteredPort, credit_poll_interval_s, poll_account_credit
from .models import OpenRouterKeySpec
from .ports import AccountPort, KeyState, OpenRouterPort, RateLimited, SecretState
from .reconcile import (
    RENEW_THRESHOLD_S,
    Create,
    NoOp,
    NormalizeSecret,
    ParkUntilReset,
    Rotate,
    SecretMissing,
    Update,
    decide,
    decide_retry,
    desired_from_spec,
    should_collect,
)

GROUP = "openrouter.teststuff.net"
VERSION = "v1alpha1"
PLURAL = "openrouterkeys"

P = ParamSpec("P")
R = TypeVar("R")

# Process-wide op counters (issue #26). Every port the handlers use is metered, so the daily
# key-API spend is measured at the only place it can be spent.
METRICS = KeyOpMetrics()

# How often the age-renewal timer looks at an expiring key (issue #25). Derived, not chosen: half
# the renewal threshold means two passes always fall inside the window, so one missed or errored
# pass still leaves another before the key dies. It is the relationship that matters — a bare
# number here could drift past the threshold and quietly reintroduce the death this fixes.
RENEW_TIMER_INTERVAL_S = RENEW_THRESHOLD_S / 2


def _now() -> datetime:
    return datetime.now(UTC)


def _port() -> OpenRouterPort:
    return MeteredPort(OpenRouterAdapter(os.environ["OPENROUTER_MANAGEMENT_KEY"]), METRICS, _now)


def _park_on_daily_limit(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn an exhausted daily key-API budget into ONE parked retry (issue #26).

    kopf's default backoff hot-retries a `keys-modify-api-rpd-*` 429 that cannot clear before UTC
    midnight — 434 429s in 15 minutes, and every deletion stuck on its finalizer. A TemporaryError
    with the delay from `decide_retry` re-queues the op once, at the reset. Non-daily 429s (and
    everything else) propagate untouched, keeping kopf's normal backoff.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except RateLimited as exc:
            plan = decide_retry(exc.limit_name, _now())
            if isinstance(plan, ParkUntilReset):
                raise kopf.TemporaryError(
                    f"OpenRouter daily key-API budget exhausted ({exc.limit_name}); parked "
                    f"{plan.delay_s:.0f}s until the UTC reset",
                    delay=plan.delay_s,
                ) from exc
            raise

    return wrapper


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
@_park_on_daily_limit
def reconcile_key(
    *,
    spec: kopf.Spec,
    status: kopf.Status,
    namespace: str | None,
    patch: kopf.Patch,
    name: str = "",
    uid: str = "",
    **_: Any,
) -> None:
    _reconcile(spec, status, namespace, patch, name, uid)


@kopf.timer(GROUP, VERSION, PLURAL, interval=RENEW_TIMER_INTERVAL_S)
@_park_on_daily_limit
def renew_aging_keys(
    *,
    spec: kopf.Spec,
    status: kopf.Status,
    namespace: str | None,
    patch: kopf.Patch,
    name: str = "",
    uid: str = "",
    **_: Any,
) -> None:
    """Periodic pass so a key can be rotated before it dies of AGE (issue #25).

    A key ageing out generates no event — the spec does not change when its clock runs down, so the
    create/update/resume handlers never fire for it, and a ride that outlasted its key's honest
    window simply 401-stormed (sleep-tracking#96). Same shape as #10's GC timer: the *decision* is
    the pure `decide()` (which renews only inside the window, and only on spend it can read), this
    is just what makes the clock reach it. Twice per renewal window, so one missed or errored pass
    still leaves a pass before the key dies.

    Only CRs that carry an `expiresAt` are polled: a standing project key has no deadline to watch,
    and reading it here would spend key-API budget on a question with no answer.
    """
    parsed = OpenRouterKeySpec.model_validate(dict(spec))
    if parsed.expires_at is None:
        return
    _reconcile(spec, status, namespace, patch, name, uid)


def _reconcile(
    spec: kopf.Spec,
    status: kopf.Status,
    namespace: str | None,
    patch: kopf.Patch,
    name: str = "",
    uid: str = "",
) -> None:
    parsed = OpenRouterKeySpec.model_validate(dict(spec))
    desired = desired_from_spec(parsed)
    if namespace is None:  # the CRD is Namespaced; satisfy the type + guard regardless
        raise kopf.PermanentError("OpenRouterKey must be namespaced")
    port = _port()

    key_hash = (status.get("openrouter") or {}).get("hash")
    observed = port.get_key(key_hash) if key_hash else None

    # Read the k8s Secret to detect drift (issue #53): a legacy/adopted Secret that is missing,
    # unlabeled, or shape-drifted must be normalized even when the upstream key is healthy.
    secret_raw = read_key_secret(namespace, parsed.target_secret_name())
    secret_state = SecretState(
        exists=secret_raw is not None,
        has_label=bool(secret_raw and SESSION_KEY_LABEL in secret_raw["labels"]),
        has_all_keys=bool(
            secret_raw and REQUIRED_DATA_KEYS.issubset(secret_raw.get("data", {}).keys())
        ),
    )
    plan = decide(
        desired,
        observed,
        secret_state,
        datetime.now(UTC),
        secret_name=parsed.target_secret_name(),
    )

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
            owner_uid=uid,
            owner_name=name,
            owner_api_version=f"{GROUP}/{VERSION}",
            owner_kind="OpenRouterKey",
        )
        patch.status["openrouter"] = _key_status(port, minted.hash)
    elif isinstance(plan, Rotate):
        # Expiry drift PATCH can't fix (issue #6), or a key about to die of age (issue #25): mint
        # fresh + swap the Secret FIRST, delete the old key LAST — a consumer never observes a
        # window without a live credential. Mint from `plan.desired`, NOT the spec-derived desired:
        # an age renewal carries a reduced cap and a fresh window in it, and minting the spec's
        # values instead would hand the session its whole budget back.
        minted = port.create_key(
            plan.desired.name,
            plan.desired.limit,
            plan.desired.reset_interval,
            plan.desired.expires_at,
        )
        write_key_secret(
            namespace,
            parsed.target_secret_name(),
            minted.value,
            minted.hash,
            parsed.guardrail or "",
            owner_uid=uid,
            owner_name=name,
            owner_api_version=f"{GROUP}/{VERSION}",
            owner_kind="OpenRouterKey",
        )
        patch.status["openrouter"] = _key_status(port, minted.hash)
        if plan.delete_old:
            # Age renewals skip this on purpose: the old key lapses on its own within the renewal
            # threshold, and deleting it now would 401 any consumer still holding a cached
            # reference to the pre-swap Secret.
            port.delete_key(plan.key_hash)
    elif isinstance(plan, Update):
        port.update_key(plan.key_hash, desired.limit, desired.reset_interval)
        patch.status["openrouter"] = _key_status(port, plan.key_hash)
    elif isinstance(plan, NormalizeSecret):
        # The upstream key is healthy, but the k8s Secret is unlabeled or shape-drifted
        # (issue #53). Read the existing key value from the Secret and rewrite it with correct
        # labels and data keys. The Secret is guaranteed to exist here — a missing Secret
        # returns SecretMissing instead, since the key value is only known at mint time.
        METRICS.record_secret_found(parsed.target_secret_name())
        patch.status.setdefault("conditions", [])
        _clear_secret_missing_condition(patch)
        assert secret_raw is not None, "NormalizeSecret requires an existing Secret"
        existing_value = secret_raw["data"].get("OPENROUTER_API_KEY", "")
        existing_hash = secret_raw["data"].get("KEY_HASH", key_hash or "")
        existing_guardrail = secret_raw["data"].get("GUARDRAIL", parsed.guardrail or "")
        write_key_secret(
            namespace,
            parsed.target_secret_name(),
            existing_value,
            existing_hash,
            existing_guardrail,
            owner_uid=uid,
            owner_name=name,
            owner_api_version=f"{GROUP}/{VERSION}",
            owner_kind="OpenRouterKey",
        )
        if observed is not None:
            patch.status["openrouter"] = _observed_status(observed)

    elif isinstance(plan, SecretMissing):
        # The upstream key is healthy, but the k8s Secret is absent (deleted out of band).
        # Surface this as a status condition + metric; do NOT re-mint (issue #56).
        METRICS.record_secret_missing(plan.secret_name)
        patch.status["conditions"] = [
            {
                "type": "SecretMissing",
                "status": "True",
                "reason": plan.secret_name,
                "message": (
                    f"k8s Secret {plan.secret_name} is absent; the upstream key is healthy. "
                    "Re-mint by deleting and re-applying the OpenRouterKey CR."
                ),
                "lastTransitionTime": datetime.now(UTC).isoformat(),
            }
        ]
        if observed is not None:
            patch.status["openrouter"] = _observed_status(observed)

    elif isinstance(plan, NoOp) and observed is not None:
        # Surface the LIVE expiry even on a no-change pass, so dispatch-time pre-flights
        # (homelab agent-session.sh: refuse a key with <30 min real life) read truth, not the spec.
        METRICS.record_secret_found(parsed.target_secret_name())
        patch.status.setdefault("conditions", [])
        _clear_secret_missing_condition(patch)
        patch.status["openrouter"] = _observed_status(observed)


def _key_status(port: OpenRouterPort, key_hash: str) -> dict[str, Any]:
    """Status block for a key we just touched: re-read it so `expires_at` is the value OpenRouter
    actually STORED (it rounds the requested instant by seconds) — the spec's wish is not status."""
    state = port.get_key(key_hash)
    if state is None:  # read-back raced/failed; the hash alone still lets the next pass reconcile
        return {"hash": key_hash}
    return _observed_status(state)


def _clear_secret_missing_condition(patch: kopf.Patch) -> None:
    """Set the SecretMissing condition to False when the Secret is present (issue #56).

    The condition is written as True in the SecretMissing handler and must be cleared when
    the Secret comes back — otherwise a restored Secret would report SecretMissing forever.
    """
    conditions = patch.status.get("conditions", [])
    for i, cond in enumerate(conditions):
        if isinstance(cond, dict) and cond.get("type") == "SecretMissing":
            conditions[i] = {
                **cond,
                "status": "False",
                "lastTransitionTime": datetime.now(UTC).isoformat(),
            }
            return
    # No existing SecretMissing condition — nothing to clear.
    conditions.append(
        {
            "type": "SecretMissing",
            "status": "False",
            "reason": "",
            "message": "k8s Secret is present.",
            "lastTransitionTime": datetime.now(UTC).isoformat(),
        }
    )


def _observed_status(state: KeyState) -> dict[str, Any]:
    return {
        "hash": state.hash,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
    }


@kopf.on.delete(GROUP, VERSION, PLURAL)
@_park_on_daily_limit
def delete_key(*, status: kopf.Status, **_: Any) -> None:
    key_hash = (status.get("openrouter") or {}).get("hash")
    if key_hash:
        _port().delete_key(key_hash)


class _MetricsHandler(BaseHTTPRequestHandler):
    """Serve the op counters at /metrics. Read-only, no request body, fixed literal labels."""

    # do_GET (not do_get): BaseHTTPRequestHandler dispatches on the method name verbatim.
    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/metrics":
            self.send_error(404)
            return
        body = METRICS.render(_now()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-scrape stderr logging — a 30s scrape would drown the operator's own log."""


@kopf.on.startup()
def start_metrics_exporter(**_: Any) -> None:
    """Stand up the /metrics endpoint (issue #26 deliverable b).

    The chart exposes only kopf's health port today, so this is the exporter surface the metrics
    Service / ServiceMonitor wiring in #27 attaches to. Bind address and port are env-configurable
    (METRICS_ADDR / METRICS_PORT, and METRICS_ENABLED=false to opt out) precisely so that chart
    wiring needs no code change here. Daemon thread: it must never hold up operator shutdown.
    """
    if os.environ.get("METRICS_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        return
    # Default 0.0.0.0: the pod network is the boundary, and #27 fronts it with a metrics Service.
    addr = os.environ.get("METRICS_ADDR", "0.0.0.0")
    server = ThreadingHTTPServer(
        (addr, int(os.environ.get("METRICS_PORT", "9090"))), _MetricsHandler
    )
    threading.Thread(target=server.serve_forever, name="metrics-exporter", daemon=True).start()
    _start_account_credit_poller()


def _start_account_credit_poller() -> None:
    """Refresh the account balance gauge on a slow timer (issue #29).

    Account credit is not a per-reconcile quantity — it changes with inference spend across the
    whole fleet, not with anything this operator does — so it is polled on its own cadence
    (METRICS_CREDIT_INTERVAL seconds, default 300, floored) instead of on the reconcile path,
    where it would add a key-API round trip to every CR event. Same METRICS_ENABLED knob: this is
    only reached when the exporter starts. Daemon thread, like the exporter.
    """
    interval_s = credit_poll_interval_s(os.environ.get("METRICS_CREDIT_INTERVAL"))

    def loop() -> None:
        while True:
            _poll_account_credit_once()
            time.sleep(interval_s)

    threading.Thread(target=loop, name="account-credit-poller", daemon=True).start()


def _poll_account_credit_once() -> None:
    """One poll, client construction included: a missing or malformed credential has to surface as
    a counted poll failure against a stale gauge, not as a thread that quietly stops existing."""
    try:
        port: AccountPort = OpenRouterAdapter(os.environ["OPENROUTER_MANAGEMENT_KEY"])
    except Exception:
        METRICS.account.record_poll_failure()
        return
    poll_account_credit(port, METRICS.account, _now)


@kopf.timer(GROUP, VERSION, PLURAL, interval=900.0)
def gc_expired_keys(
    *,
    spec: kopf.Spec,
    name: str,
    namespace: str | None,
    **_: Any,
) -> None:
    """Periodic sweep (issue #10): collect an expired ephemeral CR and its Secret.

    Nothing generates an event when the clock passes `expiresAt` (the spec never changes), so the
    reconcile handlers would never fire for an expired CR. This timer is the thin caller: the
    *decision* lives in the pure `reconcile.should_collect` (decision-table tested); here we only
    do the I/O — delete the Secret (404-tolerant; newer Secrets have ownerReferences for k8s GC)
    then the CR itself.
    """
    parsed = OpenRouterKeySpec.model_validate(dict(spec))
    if not should_collect(parsed, datetime.now(UTC)):
        return
    if namespace is None:
        raise kopf.PermanentError("OpenRouterKey must be namespaced")
    delete_key_secret(namespace, parsed.target_secret_name())
    delete_cr(GROUP, VERSION, PLURAL, namespace, name)
