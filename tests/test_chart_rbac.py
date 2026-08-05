r"""Tests for chart/templates/rbac.yaml — the RBAC rules the operator runs under.

These render the chart with ``helm template`` and verify the verb lists directly
against the *rendered* YAML, rather than parsing the raw template with a
``{{ … }}``-stripping regex.

Issue #14: the GC timer (issue #10) calls delete_namespaced_secret and
delete_namespaced_custom_object, but the chart granted no `delete` verb — 403 every tick.

Issue #18: the previous test stripped Helm ``{{ … }}`` expressions with a single-line
non-greedy regex (``r"\{\{.*?\}\}"``) that cannot survive a multi-line ``{{- if … }}``
… ``{{- end }}`` control block. We now assert against real ``helm template`` output,
which is robust to control flow in the template.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile

import pytest
import yaml

_REPO = pathlib.Path(__file__).resolve().parent.parent
_CHART = _REPO / "chart"

# The legacy helper removed by issue #18. Kept only to document (and regression-guard)
# the blind spot it had: a single-line, non-greedy regex cannot span a multi-line Helm
# control block.
_LEGACY_HELM_BLOCK_RE = re.compile(r"\{\{.*?\}\}")


def _helm_template(chart: pathlib.Path = _CHART) -> str:
    """Render *chart* with ``helm template`` and return the rendered manifest."""
    result = subprocess.run(
        ["helm", "template", str(chart)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _cluster_role_rules(manifest: str) -> list[dict[str, object]]:
    """Return the ``rules`` list from the ClusterRole in *manifest*."""
    docs = list(yaml.safe_load_all(manifest))
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "ClusterRole":
            rules = doc.get("rules")
            if isinstance(rules, list):
                return [rule for rule in rules if isinstance(rule, dict)]
    raise AssertionError("ClusterRole not found in rendered chart")


def _verbs_for(manifest: str, resources: list[str]) -> set[str]:
    """Collect the union of verbs across all rules that mention *any* of *resources*."""
    verbs: set[str] = set()
    for rule in _cluster_role_rules(manifest):
        rule_resources = rule.get("resources", [])
        if isinstance(rule_resources, list) and any(r in rule_resources for r in resources):
            verbs_list = rule.get("verbs", [])
            if isinstance(verbs_list, list):
                verbs.update(v for v in verbs_list if isinstance(v, str))
    return verbs


@pytest.mark.parametrize(
    (
        "description",
        "resources",
        "required_verb",
    ),
    [
        (
            "openrouterkeys rule must grant delete (issue #14)",
            ["openrouterkeys", "openrouterkeys/status"],
            "delete",
        ),
        (
            "secrets rule must grant delete (issue #14)",
            ["secrets"],
            "delete",
        ),
    ],
)
def test_rbac_verbs(description: str, resources: list[str], required_verb: str) -> None:
    verbs = _verbs_for(_helm_template(), resources)
    assert required_verb in verbs, (
        f"{description}: verbs for {resources} are {sorted(verbs)}, missing {required_verb!r}"
    )


# Issue #18: the legacy regex could not handle a multi-line Helm control block. The two tests
# below pin that blind spot and prove the ``helm template`` approach is robust to it.

_MULTILINE_BLOCK = (
    "rules:\n"
    "{{- if .Values.foo }}\n"
    '  - apiGroups: [""]\n'
    '    resources: ["secrets"]\n'
    '    verbs: ["get", "delete"]\n'
    "{{- end }}\n"
)


def test_legacy_regex_cannot_strip_multiline_block() -> None:
    """Old single-line regex yields invalid YAML for a multi-line block (#18 blind spot)."""
    stripped = _LEGACY_HELM_BLOCK_RE.sub('""', _MULTILINE_BLOCK)
    # Each ``{{ … }}`` is replaced with ``""`` in isolation, so ``rules:`` ends up with a scalar
    # ``""`` followed by a sequence — not valid YAML. The old test would therefore raise here.
    with pytest.raises(yaml.YAMLError):
        list(yaml.safe_load_all(stripped))


def test_helm_template_handles_multiline_block() -> None:
    """Rendering via ``helm template`` yields valid YAML even with multi-line control flow."""
    with tempfile.TemporaryDirectory() as tmp:
        chart_dir = pathlib.Path(tmp) / "chart"
        (chart_dir / "templates").mkdir(parents=True)
        (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: rbac-probe\nversion: 0.1.0\n")
        (chart_dir / "templates" / "rbac.yaml").write_text(
            "apiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: ClusterRole\n"
            "metadata:\n"
            "  name: probe\n"
            "rules:\n"
            "{{- if .Values.foo }}\n"
            '  - apiGroups: [""]\n'
            '    resources: ["secrets"]\n'
            '    verbs: ["get", "delete"]\n'
            "{{- end }}\n"
            '  - apiGroups: [""]\n'
            '    resources: ["configmaps"]\n'
            '    verbs: ["get"]\n'
        )
        manifest = _helm_template(chart_dir)
        rules = _cluster_role_rules(manifest)
        rendered_resources = [rule.get("resources") for rule in rules]
        # The static rule is always present; the conditional block renders without breaking YAML.
        assert ["configmaps"] in rendered_resources
