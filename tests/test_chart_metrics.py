r"""Tests for the metrics wiring in ``chart/`` — Service/ServiceMonitor/PrometheusRule (issue #27).

Same approach as ``tests/test_chart_rbac.py`` (issue #18): assert against real ``helm template``
output, never a ``{{ … }}``-stripping regex and never a live cluster.

Issue #26 landed the *instrumentation* (``KeyOpMetrics`` + the ``/metrics`` exporter) but its
recipe forbade ``chart/``, so the counters were unscrapeable and the exhaustion they measure was
still unalertable. This is that wiring.

Two constraints are pinned here because they are the ones that fail silently in the cluster:

* **severity is ``warning``, never ``info``** — the stock InfoInhibitor rule suppresses ``info``
  alerts, so an ``info`` alert is a rule that renders, lints, and never reaches anybody (⚖
  homelab#163, verified live 2026-08-06).
* **no alert ships without a series behind it** — ``test_every_alert_has_a_series_behind_it``
  checks each metric an ``expr`` names against what ``KeyOpMetrics.render()`` actually exposes.
  That guard is why deliverable 3's account-balance low-water alert could not ship with #27: its
  gauge was deferred to #29, so the rule would have evaluated against nothing and never fired.

#29 landed that gauge (PR #35), and ``KeyOpMetrics.render()`` composes
``AccountCreditMetrics.render()`` into the same exposition precisely so this guard keeps seeing
one surface — so the balance alert ships here as #33, with no change to the guard itself. Its own
cases pin a third failure mode the other two do not cover: a mark that *scales* has several ways
to end up unsatisfiable (a top-up inverting the trend, an idle account, a dead poller), and an
alert that cannot fire is indistinguishable from a healthy system.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from openrouter_operator.metrics import KeyOpMetrics

_REPO = pathlib.Path(__file__).resolve().parent.parent
_CHART = _REPO / "chart"
_NAMESPACE = "openrouter-operator"

# Any Prometheus series this chart's alerts are allowed to reference must be one the operator
# actually exposes. Matches the metric-name position of an expr token.
_METRIC_RE = re.compile(r"\bopenrouter_[a-zA-Z0-9_]*")


def _helm_template(*set_values: str) -> list[dict[str, Any]]:
    """Render the chart with ``helm template`` and return the parsed manifests."""
    cmd = ["helm", "template", "openrouter-operator", str(_CHART), "--namespace", _NAMESPACE]
    for value in set_values:
        cmd += ["--set", value]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _doc(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Return the single manifest of *kind*, asserting it was rendered exactly once."""
    matches = [d for d in docs if d.get("kind") == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def _dig(doc: Any, path: tuple[str | int, ...]) -> Any:
    """Walk *path* (mapping keys and list indices) into *doc*."""
    cursor = doc
    for step in path:
        cursor = cursor[step]
    return cursor


def _alert_rules(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every alerting rule across every group of the rendered PrometheusRule."""
    groups = _dig(_doc(docs, "PrometheusRule"), ("spec", "groups"))
    return [rule for group in groups for rule in group.get("rules", []) if "alert" in rule]


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    """Default values — monitoring is default-on (the operator always ships with it present)."""
    return _helm_template()


@pytest.mark.parametrize(
    ("case", "kind", "path", "expected"),
    [
        # -- deliverable 1: a ClusterIP Service fronting the metrics port -------------------
        ("service-is-clusterip", "Service", ("spec", "type"), "ClusterIP"),
        ("service-port-is-named-metrics", "Service", ("spec", "ports", 0, "name"), "metrics"),
        ("service-exposes-the-exporter-port", "Service", ("spec", "ports", 0, "port"), 9090),
        # by NAME, so re-pointing metrics.port moves container + Service together
        (
            "service-targets-the-container-port-by-name",
            "Service",
            ("spec", "ports", 0, "targetPort"),
            "metrics",
        ),
        (
            "service-selects-the-operator-pod",
            "Service",
            ("spec", "selector", "app"),
            "openrouter-operator",
        ),
        # -- deliverable 2: scrape config --------------------------------------------------
        (
            "servicemonitor-uses-the-prometheus-operator-api",
            "ServiceMonitor",
            ("apiVersion",),
            "monitoring.coreos.com/v1",
        ),
        (
            "servicemonitor-scrapes-the-named-port",
            "ServiceMonitor",
            ("spec", "endpoints", 0, "port"),
            "metrics",
        ),
        (
            "servicemonitor-scrapes-the-exporter-path",
            "ServiceMonitor",
            ("spec", "endpoints", 0, "path"),
            "/metrics",
        ),
        (
            "servicemonitor-selects-the-metrics-service",
            "ServiceMonitor",
            ("spec", "selector", "matchLabels", "component"),
            "metrics",
        ),
        (
            "servicemonitor-is-scoped-to-the-release-namespace",
            "ServiceMonitor",
            ("spec", "namespaceSelector", "matchNames", 0),
            _NAMESPACE,
        ),
        # -- deliverable 3: the key-ops/day alert (balance alert withheld — see module docstring)
        (
            "rule-group-is-named-by-subsystem",
            "PrometheusRule",
            ("spec", "groups", 0, "name"),
            "openrouter-operator.key-api",
        ),
        (
            "alert-names-the-symptom",
            "PrometheusRule",
            ("spec", "groups", 0, "rules", 0, "alert"),
            "OpenRouterKeyOpsDailyBudgetNearlyExhausted",
        ),
        (
            "alert-severity-is-warning-never-info",
            "PrometheusRule",
            ("spec", "groups", 0, "rules", 0, "labels", "severity"),
            "warning",
        ),
        (
            "alert-is-marked-no-agent-triage",
            "PrometheusRule",
            ("spec", "groups", 0, "rules", 0, "labels", "triage"),
            "none",
        ),
        # -- deliverable 3, second half: the account-balance low-water alert (issue #33) -----
        # Its own group: the balance is account scope, not key-API scope — a different subsystem
        # and a different remedy (top up credit vs wait out the UTC reset).
        (
            "balance-rule-group-is-named-by-subsystem",
            "PrometheusRule",
            ("spec", "groups", 1, "name"),
            "openrouter-operator.account",
        ),
        (
            "balance-alert-names-the-symptom",
            "PrometheusRule",
            ("spec", "groups", 1, "rules", 0, "alert"),
            "OpenRouterAccountCreditNearlyExhausted",
        ),
        (
            "balance-alert-severity-is-warning-never-info",
            "PrometheusRule",
            ("spec", "groups", 1, "rules", 0, "labels", "severity"),
            "warning",
        ),
        (
            "balance-alert-is-marked-no-agent-triage",
            "PrometheusRule",
            ("spec", "groups", 1, "rules", 0, "labels", "triage"),
            "none",
        ),
    ],
)
def test_rendered_manifest_field(
    rendered: list[dict[str, Any]], case: str, kind: str, path: tuple[str | int, ...], expected: Any
) -> None:
    assert _dig(_doc(rendered, kind), path) == expected, case


def test_deployment_exposes_and_configures_the_exporter(rendered: list[dict[str, Any]]) -> None:
    """The Service targets ``metrics`` by name, so the container must declare that port, and the
    exporter's env knobs must follow the chart's values (operator.py reads METRICS_PORT/_ADDR)."""
    container = _dig(_doc(rendered, "Deployment"), ("spec", "template", "spec", "containers", 0))
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    assert ports.get("metrics") == 9090
    assert ports.get("health") == 8080, "the existing kopf health port must survive"
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env.get("METRICS_PORT") == "9090"


def test_alert_thresholds_the_modify_ops_against_the_rpd_ceiling(
    rendered: list[dict[str, Any]],
) -> None:
    """``keys-modify-api-rpd-v2`` bounds the *modify* ops. ``get`` shares the metric but not the
    budget, so counting it would fire the alert on pure reconcile churn."""
    expr = _alert_rules(rendered)[0]["expr"]
    assert "openrouter_key_api_ops_today" in expr
    assert 'op=~"create|update|delete"' in expr, f"must exclude the read op: {expr}"
    assert "> 700" in expr, f"70% of the default 1000/day ceiling: {expr}"


@pytest.mark.parametrize(
    ("case", "ceiling", "percent", "expected_threshold"),
    [
        ("default ceiling at 70%", 1000, 70, "> 700"),
        ("a lower ceiling scales the threshold", 500, 70, "> 350"),
        ("the warn percentage is tunable", 1000, 80, "> 800"),
    ],
)
def test_alert_threshold_is_derived_from_values(
    case: str, ceiling: int, percent: int, expected_threshold: str
) -> None:
    docs = _helm_template(
        f"metrics.prometheusRule.keyOpsPerDay.ceiling={ceiling}",
        f"metrics.prometheusRule.keyOpsPerDay.warnAtPercent={percent}",
    )
    assert expected_threshold in _alert_rules(docs)[0]["expr"], case


def _expr(docs: list[dict[str, Any]], alert: str) -> str:
    """The named alert's ``expr``, whitespace-collapsed so YAML folding is not part of the
    assertion — only the PromQL is."""
    matches = [r for r in _alert_rules(docs) if r["alert"] == alert]
    assert len(matches) == 1, f"expected exactly one {alert}, got {len(matches)}"
    return " ".join(str(matches[0]["expr"]).split())


_BALANCE_ALERT = "OpenRouterAccountCreditNearlyExhausted"


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        # #29 ships no usage/spend counter, only the balance gauge — so trailing burn has to come
        # from that gauge's own decrease. There is no second series to rate() against.
        (
            "burn comes from the balance gauge's own 24h decrease",
            "-delta(openrouter_account_credit_usd[24h])",
        ),
        # Silent-never-fire #1: a top-up RAISES the gauge, so the delta goes positive and the
        # derived burn negative — `balance < multiplier * negative` is unsatisfiable at any
        # positive balance. Clamping the burn term at zero collapses that to "no burn known".
        (
            "a top-up cannot invert the self-scaling mark",
            "clamp_min(-delta(openrouter_account_credit_usd[24h]), 0)",
        ),
        # Silent-never-fire #2: "no burn known" (or a freshly restarted operator) leaves a ZERO
        # mark, equally unsatisfiable. The absolute floor is a separate disjunct rather than a
        # clamp_min around the whole mark, because clamp_min(NaN, floor) is NaN in PromQL — the
        # gauge reads NaN until the first successful poll, and that NaN would swallow the floor
        # for the whole 24h the range covers.
        ("an absolute floor fires on its own", "or openrouter_account_credit_usd < 5"),
        # The false-alarm direction: a reading nobody has refreshed is not evidence of a drained
        # account. metrics.py documents this idiom; the timestamp is 0 when no poll ever
        # succeeded, so a never-polled operator reads as maximally stale and cannot page.
        (
            "a stale or never-polled reading cannot page",
            "(time() - openrouter_account_credit_updated_timestamp_seconds) < 3600",
        ),
    ],
)
def test_balance_alert_survives_the_ways_it_could_silently_never_fire(
    rendered: list[dict[str, Any]], case: str, fragment: str
) -> None:
    """The low-water mark scales with spend, so every term that could quietly zero it out — a
    top-up, an idle account, a dead poller — is pinned here rather than left to review."""
    assert fragment in _expr(rendered, _BALANCE_ALERT), case


@pytest.mark.parametrize(
    ("case", "setting", "fragment"),
    [
        ("the burn multiplier is tunable", "burnMultiplier=3", "usd < 3 * clamp_min("),
        ("the absolute floor is tunable", "floorUsd=25", "or openrouter_account_credit_usd < 25"),
        ("the freshness window is tunable", "maxStalenessSeconds=900", "seconds) < 900"),
    ],
)
def test_balance_alert_mark_is_derived_from_values(case: str, setting: str, fragment: str) -> None:
    docs = _helm_template(f"metrics.prometheusRule.accountBalance.{setting}")
    assert fragment in _expr(docs, _BALANCE_ALERT), case


def test_every_alert_has_a_series_behind_it(rendered: list[dict[str, Any]]) -> None:
    """A rule whose metric nothing exposes never fires — it reads as 'healthy' forever, which is
    the exact failure #26 was filed for. This is why the balance low-water alert is withheld
    until #29 lands its gauge."""
    exposed = KeyOpMetrics().render(datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    for rule in _alert_rules(rendered):
        for metric in _METRIC_RE.findall(str(rule["expr"])):
            assert f"{metric}{{" in exposed or f"{metric} " in exposed, (
                f"{rule['alert']} references {metric}, which the operator does not expose"
            )


def test_no_alert_is_suppressible_and_all_describe_a_symptom(
    rendered: list[dict[str, Any]],
) -> None:
    rules = _alert_rules(rendered)
    assert rules, "the PrometheusRule must ship at least one alert"
    for rule in rules:
        assert rule["labels"]["severity"] == "warning", (
            f"{rule['alert']}: info alerts are swallowed by the cluster's InfoInhibitor"
        )
        assert rule["annotations"]["summary"]
        assert rule["annotations"]["description"]
        assert rule["for"]


@pytest.mark.parametrize(
    ("case", "kind"),
    [
        ("no Service when metrics are off", "Service"),
        ("no scrape config when metrics are off", "ServiceMonitor"),
        ("no rules when metrics are off", "PrometheusRule"),
    ],
)
def test_metrics_objects_are_toggleable(case: str, kind: str) -> None:
    docs = _helm_template("metrics.enabled=false")
    assert [d for d in docs if d.get("kind") == kind] == [], case
    container = _dig(_doc(docs, "Deployment"), ("spec", "template", "spec", "containers", 0))
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env.get("METRICS_ENABLED") == "false", "the exporter must be told to stay down too"
    assert all(p["name"] != "metrics" for p in container["ports"])
