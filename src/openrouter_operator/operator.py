"""kopf handlers — thin glue: parse spec -> observe -> decide() -> apply via the port.

All the judgement lives in reconcile.decide() (pure, tested). This module only does I/O.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ParamSpec, TypeVar

import kopf

from .adapter import OpenRouterAdapter
from .k8s import delete_cr, delete_key_secret, write_key_secret
from .metrics import KeyOpMetrics, MeteredPort
from .models import OpenRouterKeySpec
from .ports import KeyState, OpenRouterPort, RateLimited
from .reconcile import (
    Create,
    NoOp,
    ParkUntilReset,
    Rotate,
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
    do the I/O — delete the Secret (404-tolerant, since `write_key_secret` sets no ownerReferences)
    then the CR itself.
    """
    parsed = OpenRouterKeySpec.model_validate(dict(spec))
    if not should_collect(parsed, datetime.now(UTC)):
        return
    if namespace is None:
        raise kopf.PermanentError("OpenRouterKey must be namespaced")
    delete_key_secret(namespace, parsed.target_secret_name())
    delete_cr(GROUP, VERSION, PLURAL, namespace, name)
