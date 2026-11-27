"""The Python version SET agrees — one major.minor across every pinned member (the homelab
`gateway/tests/test_python_version_set.py` shape; homelab agents/lenses/migration.md §Version SETS).

CI runs under the devbox interpreter, the image runs the Dockerfile's — a Renovate bump that moves
only the image `FROM` leaves `ci` proving the OLD interpreter and says nothing about the new one,
and a requires-python / ruff target / mypy python_version left behind lints and type-checks for a
runtime the image no longer ships. Renovate starts each bump (the Dockerfile `FROM`); this row makes
a one-member move red, naming the odd member(s), so the rest of the set follows in the same PR.
Static text scan: no network, no Docker, no interpreter launch.

Decision table (test id = row description):

| row | probe | expect |
|---|---|---|
| the Python version set agrees | every member's major.minor | one value |
| bumping only `<member>` is red | that member set to 9.99 | fails, naming member + majority |
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_PY = r"(\d+\.\d+)"
# devbox's search index can lag nixpkgs, so the devbox members also read the nixpkgs FLAKE-REF form
# `github:NixOS/nixpkgs/<rev>#python3NN` → "3.NN"; the plain `python@X.Y` form is the current one.
_FLAKE_PY = r'"?github:NixOS/nixpkgs/[0-9a-f]+#python(\d)(\d{2,})'


def _swap(m: re.Match[str]) -> str:
    """Odd-value mutation for a single `X.Y` group: rewrite it to 9.99."""
    text, base = m.group(0), m.start(0)
    lo, hi = m.span(1)
    return text[: lo - base] + "9.99" + text[hi - base :]


def _mutate_any(m: re.Match[str]) -> str:
    """Odd-value mutation for a member with alternative forms: rewrite the matched version groups
    so they read 9.99 — "X.Y" → "9.99", flake `python3NN` → `python999`."""
    spans = [m.span(i) for i, g in enumerate(m.groups(), 1) if g is not None]
    repl = ["9.99"] if len(spans) == 1 else ["9", "99"]
    text, base = m.group(0), m.start(0)
    for (lo, hi), r in reversed(list(zip(spans, repl, strict=True))):
        text = text[: lo - base] + r + text[hi - base :]
    return text


# member -> (repo file, regex whose groups spell the major.minor, odd-value mutation of the match)
_MEMBERS: dict[str, tuple[str, str, Callable[[re.Match[str]], str]]] = {
    "Dockerfile FROM": ("Dockerfile", rf"^FROM python:{_PY}-slim\b", _swap),
    "devbox.json python@": ("devbox.json", rf'(?:"python@{_PY}"|{_FLAKE_PY}")', _mutate_any),
    "devbox.lock python@": ("devbox.lock", rf'^\s*"(?:python@{_PY}|{_FLAKE_PY})":', _mutate_any),
    "pyproject.toml requires-python": (
        "pyproject.toml",
        rf'^requires-python\s*=\s*">=\s*{_PY}',
        _swap,
    ),
    "pyproject.toml ruff target-version": (
        "pyproject.toml",
        r'^target-version\s*=\s*"py(\d)(\d{2,})"',
        lambda m: 'target-version = "py999"',
    ),
    "pyproject.toml mypy python_version": (
        "pyproject.toml",
        rf'^python_version\s*=\s*"{_PY}"',
        _swap,
    ),
    "uv.lock requires-python": ("uv.lock", rf'^requires-python\s*=\s*">=\s*{_PY}', _swap),
}


def _python_versions(texts: dict[str, str]) -> dict[str, str]:
    out = {}
    for member, (fname, pattern, _) in _MEMBERS.items():
        m = re.search(pattern, texts[fname], re.MULTILINE)
        out[member] = ".".join(g for g in m.groups() if g is not None) if m else "<not found>"
    return out


def _python_set_problem(versions: dict[str, str]) -> str | None:
    if len(set(versions.values())) == 1:
        return None
    counts: dict[str, int] = {}
    for v in versions.values():
        counts[v] = counts.get(v, 0) + 1
    majority = max(counts, key=lambda v: (counts[v], v))
    odd = [m for m, v in versions.items() if v != majority]
    lines = [f"  {m}: {v}" for m, v in versions.items()]
    return (
        "the Python version set must agree on one major.minor — move every member together "
        "(Dockerfile FROM, devbox.json + devbox.lock, requires-python, ruff target-version, "
        "mypy python_version, uv.lock — re-lock with `devbox run -- uv lock`):\n"
        + "\n".join(lines)
        + f"\nmajority={majority}; odd one(s) out: {', '.join(odd)}"
    )


def _real_texts() -> dict[str, str]:
    return {f: (REPO / f).read_text(encoding="utf-8") for f in {v[0] for v in _MEMBERS.values()}}


def test_python_version_set_agrees() -> None:
    """row: the Python version set agrees — every pinned member reads one major.minor."""
    versions = _python_versions(_real_texts())
    assert "<not found>" not in versions.values(), f"a member pattern no longer matches: {versions}"
    problem = _python_set_problem(versions)
    assert problem is None, problem


@pytest.mark.parametrize("member", list(_MEMBERS), ids=list(_MEMBERS))
def test_python_version_set_mutation_names_member(member: str) -> None:
    """row: bumping only this member is red, naming it and the majority's value."""
    texts = _real_texts()
    fname, pattern, mutate = _MEMBERS[member]
    base = _python_versions(texts)[member]
    assert base != "<not found>", f"{member}: pattern no longer matches {fname}"
    texts[fname] = re.sub(pattern, mutate, texts[fname], count=1, flags=re.MULTILINE)
    problem = _python_set_problem(_python_versions(texts))
    assert problem is not None and f"odd one(s) out: {member}" in problem
    assert f"majority={base}" in problem and f"{member}: 9.99" in problem
