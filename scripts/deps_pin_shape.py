"""deps-pin-shape — the pin-only diff test for the Python dependency files, called by
scripts/deps-pin-guard.sh (the CODEOWNERS replacement owner for pyproject.toml + uv.lock,
operator direction 2026-09-27).

    python3 scripts/deps_pin_shape.py pyproject <base-file> <head-file>
    python3 scripts/deps_pin_shape.py uv-lock   <base-file> <head-file>

Exit 0 when the head differs from the base ONLY in pin material; exit 1 with the offending lines
on stderr otherwise. Everything not recognised as pin material is a refusal — the check fails
closed, and a refused PR simply takes the owned route (a human reads it).

Both checks work the same way: reduce each file to a SKELETON in which every pin-shaped line is
replaced by a placeholder (or normalised), then demand skeleton(base) == skeleton(head). Whatever
survives the reduction is structure — table headers, package names, dependency graphs, policy
lines — and structure must not move in a dep-bump PR.

pyproject.toml — pin material is a dependency specifier string — one per line in a multi-line
array, or the string elements of a single-line array (`requires = ["hatchling"]`) — inside one of
the dependency arrays ([project] dependencies, [project.optional-dependencies] *, [dependency-
groups] *, [tool.uv] dev-dependencies / constraint-dependencies, [build-system] requires). The line
is reduced to its normalised package name, so ONLY the version constraint may change: a new or
removed package changes the name multiset and is refused; `requires-python`, `[project] version`,
tool tables, comments and formatting are structure. A specifier line carrying a trailing comment is
deliberately not pin material (fail closed).

uv.lock — pin material is the resolver's per-package version + artifact lines: `version = "…"`,
`revision = N`, `sdist = { url, hash, size[, upload-time] }`, the wheel entries in `wheels = [ … ]`,
and the `specifier = "…"` inside `[package.metadata]` requires-dist / requires-dev entries (the
lock's copy of the pyproject constraint — normalised so the NAME must stay). The `[[package]]`
headers with their `name = "…"` and `source = …` lines, every `dependencies = [ { name = … } ]`
graph entry, `requires-python` and `resolution-markers` are structure: a package added or removed
(a new transitive dependency uv pulled in for a bump) is refused and parks for the human. That is
the STRICT rule, chosen deliberately — "every added package is a transitive dep of a bumped one"
cannot be verified from the diff alone without re-resolving, and an unverifiable relaxation is
not an owner.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

# ── pyproject.toml ──────────────────────────────────────────────────────────────────────────────

# A TOML table header: [project], [project.optional-dependencies], [[tool.mypy.overrides]] …
_TABLE_RE = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-\"']+)\s*\]\]?\s*$")
# The opening line of a multi-line array: `dependencies = [`
_ARRAY_OPEN_RE = re.compile(r"^\s*([A-Za-z0-9_\-\"']+)\s*=\s*\[\s*$")
_ARRAY_CLOSE_RE = re.compile(r"^\s*\]\s*,?\s*$")
# A single-line array: `requires = ["hatchling"]`, `sdk = ["openrouter", "httpx>=0.28"]`.
_ARRAY_INLINE_RE = re.compile(
    r"^(?P<lead>\s*)(?P<key>[A-Za-z0-9_\-\"']+)\s*=\s*\[(?P<body>.*)\]\s*$"
)
_STRING_TOKEN_RE = re.compile(r"\"[^\"]*\"")
# One PEP 508 requirement string on its own line: `"kopf>=1.37",` / `"openrouter"` /
# `"foo[extra]~=1.2; python_version < '3.15'",`. No trailing comment (fail closed).
_SPEC_LINE_RE = re.compile(
    r"^\s*\"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?)"
    r"(?P<extras>\[[A-Za-z0-9._,\s\-]*\])?"
    r"(?P<rest>\s*(?:[<>=!~][^\"]*)?)\",?\s*$"
)

# (table, array) pairs whose string entries are dependency specifiers. `*` = any array key.
_PYPROJECT_DEP_ARRAYS: dict[str, frozenset[str]] = {
    "project": frozenset({"dependencies"}),
    "project.optional-dependencies": frozenset({"*"}),
    "dependency-groups": frozenset({"*"}),
    "tool.uv": frozenset({"dev-dependencies", "constraint-dependencies"}),
    "build-system": frozenset({"requires"}),
}


def _norm_name(name: str) -> str:
    """PEP 503 normalisation: case-insensitive, runs of `-_.` collapse to `-`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_dep_array(table: str, array: str) -> bool:
    allowed = _PYPROJECT_DEP_ARRAYS.get(table)
    return allowed is not None and ("*" in allowed or array in allowed)


def _inline_dep_array(table: str, line: str, names: Counter[str]) -> str | None:
    """Reduce `key = ["a>=1", "b"]` in a dep table to its skeleton, counting the names; None when
    the line is not a well-formed single-line array of specifier strings (then it is structure)."""
    m = _ARRAY_INLINE_RE.match(line)
    if not m:
        return None
    array = m.group("key").strip("\"'")
    if not _is_dep_array(table, array):
        return None
    body = m.group("body")
    tokens = _STRING_TOKEN_RE.findall(body)
    if _STRING_TOKEN_RE.sub("", body).strip(", \t") != "":
        return None  # something other than strings and commas inside the brackets
    keys: list[str] = []
    for tok in tokens:
        sm = _SPEC_LINE_RE.match(tok)
        if not sm:
            return None
        keys.append(f"{table}/{array}/{_norm_name(sm.group('name'))}")
    for key in keys:
        names[key] += 1
    return f"{m.group('lead')}{m.group('key')} = [{', '.join(f'<dep {k}>' for k in keys)}]"


def _pyproject_reduce(text: str) -> tuple[list[str], Counter[str]]:
    """Return (skeleton lines, multiset of `table/array/name` keys for the dep lines)."""
    skeleton: list[str] = []
    names: Counter[str] = Counter()
    table = ""
    array: str | None = None
    for line in text.splitlines():
        m = _TABLE_RE.match(line)
        if m:
            table, array = m.group(1).strip("\"'"), None
            skeleton.append(line)
            continue
        if array is None:
            m = _ARRAY_OPEN_RE.match(line)
            if m:
                array = m.group(1).strip("\"'")
                skeleton.append(line)
                continue
            inline = _inline_dep_array(table, line, names)
            if inline is not None:
                skeleton.append(inline)
                continue
        elif _ARRAY_CLOSE_RE.match(line):
            array = None
            skeleton.append(line)
            continue
        if array is not None and _is_dep_array(table, array):
            m = _SPEC_LINE_RE.match(line)
            if m:
                key = f"{table}/{array}/{_norm_name(m.group('name'))}"
                names[key] += 1
                skeleton.append(f"<dep {key}>")
                continue
        skeleton.append(line)
    return skeleton, names


def check_pyproject(base: str, head: str) -> list[str]:
    """Return the refusal reasons (empty = pin-only)."""
    base_skel, base_names = _pyproject_reduce(base)
    head_skel, head_names = _pyproject_reduce(head)
    reasons = _skeleton_diff(base_skel, head_skel)
    for key, n in (head_names - base_names).items():
        reasons.append(f"+ new dependency: {key} (x{n})")
    for key, n in (base_names - head_names).items():
        reasons.append(f"- removed dependency: {key} (x{n})")
    return reasons


# ── uv.lock ─────────────────────────────────────────────────────────────────────────────────────

_UV_PIN_LINE_RES = (
    re.compile(r"^version = \"[^\"]+\"$"),
    re.compile(r"^revision = [0-9]+$"),
    re.compile(
        r"^sdist = \{ url = \"[^\"]+\", hash = \"[a-z0-9]+:[0-9a-f]+\", size = [0-9]+"
        r"(?:, upload-time = \"[^\"]+\")? \}$"
    ),
    re.compile(
        r"^    \{ url = \"[^\"]+\", hash = \"[a-z0-9]+:[0-9a-f]+\", size = [0-9]+"
        r"(?:, upload-time = \"[^\"]+\")? \},$"
    ),
)
# `{ name = "kopf", specifier = ">=1.37" },` — keep the name, normalise the constraint.
_UV_SPECIFIER_RE = re.compile(r"(?<=[ ,]specifier = )\"[^\"]*\"")


def _uv_lock_reduce(text: str) -> list[str]:
    """Pin lines collapse to ONE `<pin>` marker per run — a new release ships a different NUMBER
    of wheels, and the wheel count is pin material, not structure."""
    skeleton: list[str] = []
    for line in text.splitlines():
        if any(r.match(line) for r in _UV_PIN_LINE_RES):
            if not skeleton or skeleton[-1] != "<pin>":
                skeleton.append("<pin>")
            continue
        skeleton.append(_UV_SPECIFIER_RE.sub('"<specifier>"', line))
    return skeleton


def check_uv_lock(base: str, head: str) -> list[str]:
    """Return the refusal reasons (empty = pin-only)."""
    return _skeleton_diff(_uv_lock_reduce(base), _uv_lock_reduce(head))


# ── shared ──────────────────────────────────────────────────────────────────────────────────────


def _skeleton_diff(base: list[str], head: list[str]) -> list[str]:
    """Lines present in exactly one skeleton — the structural changes, with a side marker."""
    if base == head:
        return []
    base_c, head_c = Counter(base), Counter(head)
    out: list[str] = []
    for line, n in (base_c - head_c).items():
        out.extend([f"- {line}"] * n)
    for line, n in (head_c - base_c).items():
        out.extend([f"+ {line}"] * n)
    if not out:
        # Same multiset, different order: a structural line moved.
        out.append("structural lines reordered (same content, different positions)")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("pyproject", "uv-lock"):
        print(__doc__, file=sys.stderr)
        return 2
    kind, base_path, head_path = argv[1], Path(argv[2]), Path(argv[3])
    base = base_path.read_text(encoding="utf-8") if base_path.exists() else ""
    head = head_path.read_text(encoding="utf-8") if head_path.exists() else ""
    reasons = check_pyproject(base, head) if kind == "pyproject" else check_uv_lock(base, head)
    if reasons:
        for r in reasons[:20]:
            print(r, file=sys.stderr)
        if len(reasons) > 20:
            print(f"… and {len(reasons) - 20} more", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
