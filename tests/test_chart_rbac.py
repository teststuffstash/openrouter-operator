"""Reproduce issue #18: the legacy ``{{ … }}``-stripping regex used by the RBAC test
cannot handle a multi-line Helm control block (``{{- if … }}`` … ``{{- end }}``).

The old test read chart/templates/rbac.yaml and stripped Helm expressions with a
single-line, non-greedy regex ``r"\\{\\{.*?\\}\\}"``. That regex cannot span
newlines, so the moment such a control block is added to the template the stripped
output is no longer valid YAML and the test breaks — even though the *rendered*
RBAC is perfectly fine. This test pins that blind spot (RED) before we swap the
assertion to real ``helm template`` output.
"""

from __future__ import annotations

import pathlib
import re
import yaml

_REPO = pathlib.Path(__file__).resolve().parent.parent
_RBAC = _REPO / "chart" / "templates" / "rbac.yaml"

# The legacy helper under test (issue #18): a single-line, non-greedy regex.
_LEGACY_HELM_BLOCK_RE = re.compile(r"\{\{.*?\}\}")

# A representative multi-line Helm control block, as would appear in rbac.yaml once
# someone guards a rule with `{{- if … }}`.
_MULTILINE_BLOCK = (
    "rules:\n"
    '{{- if .Values.foo }}\n'
    '  - apiGroups: [""]\n'
    '    resources: ["secrets"]\n'
    '    verbs: ["get", "delete"]\n'
    "{{- end }}\n"
)


def test_legacy_helper_cannot_strip_multiline_block() -> None:
    """The legacy regex must fully strip a multi-line control block (desired, currently broken)."""
    stripped = _LEGACY_HELM_BLOCK_RE.sub('""', _MULTILINE_BLOCK)
    docs = list(yaml.safe_load_all(stripped))
    rules: list[dict[str, object]] = []
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "ClusterRole":
            rules = [r for r in doc.get("rules", []) if isinstance(r, dict)]
    resources = [res for r in rules for res in (r.get("resources") or [])]
    assert "secrets" in resources
