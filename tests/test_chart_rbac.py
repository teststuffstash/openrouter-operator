"""Smoke-tests for chart/templates/rbac.yaml — the RBAC rules the operator runs under.

These read the *raw* template (not helm-rendered) and verify the verb lists directly.
The rules section contains no Helm template interpolation, but the ServiceAccount/Binding
metadata does — so we strip ``{{ … }}`` blocks before parsing.

Issue #14: the GC timer (issue #10) calls delete_namespaced_secret and
delete_namespaced_custom_object, but the chart granted no `delete` verb — 403 every tick.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

_REPO = pathlib.Path(__file__).resolve().parent.parent
_RBAC = _REPO / "chart" / "templates" / "rbac.yaml"

_HELM_BLOCK_RE = re.compile(r"\{\{.*?\}\}")


def _rendered_yaml() -> str:
    """Strip Helm ``{{ … }}`` expressions so the template parses as plain YAML."""
    return _HELM_BLOCK_RE.sub('""', _RBAC.read_text())


def _cluster_role_rules() -> list[dict[str, object]]:
    """Return the ``rules`` list from the ClusterRole in the raw template."""
    docs = list(yaml.safe_load_all(_rendered_yaml()))
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "ClusterRole":
            rules = doc.get("rules")
            if isinstance(rules, list):
                return [rule for rule in rules if isinstance(rule, dict)]
    raise AssertionError("ClusterRole not found in rbac.yaml")


def _verbs_for(resources: list[str]) -> set[str]:
    """Collect the union of verbs across all rules that mention *any* of *resources*."""
    verbs: set[str] = set()
    for rule in _cluster_role_rules():
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
    verbs = _verbs_for(resources)
    assert required_verb in verbs, (
        f"{description}: verbs for {resources} are {sorted(verbs)}, missing {required_verb!r}"
    )
