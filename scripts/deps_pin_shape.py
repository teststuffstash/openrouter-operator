"""deps-pin-shape — the pin-only diff test for the Python dependency files, called by
scripts/deps-pin-guard.sh (the CODEOWNERS replacement owner for pyproject.toml + uv.lock,
operator direction 2026-09-27).

    python3 scripts/deps_pin_shape.py pyproject <base-file> <head-file> [<head-Dockerfile>]
    python3 scripts/deps_pin_shape.py uv-lock   <base-file> <head-file> [<head-Dockerfile>]

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
removed package changes the name multiset and is refused; `[project] version`, tool tables,
comments and formatting are structure. A specifier line carrying a trailing comment is
deliberately not pin material (fail closed).

The Python version SET is pin material only as a whole. Its pyproject members are
`[project] requires-python = ">=X.Y"`, `[tool.ruff] target-version = "pyXY"` and
`[tool.mypy] python_version = "X.Y"` — exactly those spellings; any other form of the line stays
structure. A member line reduces to a placeholder (its POSITION is still structure) and its X.Y is
collected; when any member's value moved, the diff is admitted only if EVERY member at the head
reads one X.Y and that X.Y equals the head Dockerfile's `FROM python:X.Y…` tag (the image Renovate
already moved — the set converging on it, tests/test_python_version_set.py's invariant). A member
moved alone, a set split across values, a set moved to anything but the image, or no readable
image tag → refused. Without the Dockerfile argument no set move is admitted (fail closed).

uv.lock — pin material is the resolver's per-package version + artifact lines: `version = "…"`,
`revision = N`, `sdist = { url, hash, size[, upload-time] }`, the wheel entries in `wheels = [ … ]`,
and the `specifier = "…"` inside `[package.metadata]` requires-dist / requires-dev entries (the
lock's copy of the pyproject constraint — normalised so the NAME must stay). A `wheels = [ … ]`
array holding only wheel lines is pin material as a whole: a release (or a raised Python floor)
can leave a package with zero matching wheels, and the wheel count is already pin material. The
header's `requires-python = ">=X.Y"` is the lock's Python-version-set member, under the same rule
as pyproject's (head value == the head image's X.Y); the header's top-level `resolution-markers`
array, when every entry is a bare `python_full_version` comparison, may change only together with
an admitted requires-python move (uv forks the resolution on the Python floor — raising it
collapses the fork). The `[[package]]`
headers with their `name = "…"` and `source = …` lines, every `dependencies = [ { name = … } ]`
graph entry, and every per-package `resolution-markers` are structure: a package added or removed
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


# The Python version SET's members in pyproject.toml: (table, key) → the one spelling read as a
# member, its groups spelling major and minor. Mirrors tests/test_python_version_set.py's members.
_PY_SET_MEMBERS: dict[tuple[str, str], re.Pattern[str]] = {
    ("project", "requires-python"): re.compile(r'^requires-python\s*=\s*">=\s*(\d+)\.(\d+)"\s*$'),
    ("tool.ruff", "target-version"): re.compile(r'^target-version\s*=\s*"py(\d)(\d+)"\s*$'),
    ("tool.mypy", "python_version"): re.compile(r'^python_version\s*=\s*"(\d+)\.(\d+)"\s*$'),
}
# `FROM python:3.15-slim`, `FROM --platform=… python:3.15.1@sha256:…`, `docker.io/library/python:`.
_DOCKER_FROM_PY_RE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?:docker\.io/)?(?:library/)?python:(\d+)\.(\d+)(?!\d)",
    re.IGNORECASE | re.MULTILINE,
)


def image_python_version(dockerfile: str | None) -> str | None:
    """The X.Y every `FROM python:` line of the Dockerfile agrees on; None when there is no such
    line or the stages disagree (no single image version to converge on)."""
    if dockerfile is None:
        return None
    found = {f"{m.group(1)}.{m.group(2)}" for m in _DOCKER_FROM_PY_RE.finditer(dockerfile)}
    return found.pop() if len(found) == 1 else None


def _set_move_reasons(
    base: dict[str, list[str]], head: dict[str, list[str]], image: str | None
) -> list[str]:
    """The Python-version-set rule shared by both files: no member moved → nothing to say; a move
    is admitted only when every head member reads the image's X.Y."""
    if base == head:
        return []
    head_values = {v for vs in head.values() for v in vs}
    if image is not None and head_values == {image}:
        return []
    reasons = [
        f"python version set: {key} {','.join(base.get(key, [])) or '<absent>'} → "
        f"{','.join(head.get(key, [])) or '<absent>'}"
        for key in sorted(set(base) | set(head))
        if base.get(key) != head.get(key)
    ]
    reasons.append(
        "python version set: a member moved, so EVERY member must read the Dockerfile's "
        f"`FROM python:` version (image: {image or '<none readable>'}; "
        f"head members: {', '.join(f'{k}={v}' for k, vs in sorted(head.items()) for v in vs)})"
    )
    return reasons


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


def _pyproject_reduce(text: str) -> tuple[list[str], Counter[str], dict[str, list[str]]]:
    """Return (skeleton lines, multiset of `table/array/name` keys for the dep lines, the Python
    version set's member values by `table/key`)."""
    skeleton: list[str] = []
    names: Counter[str] = Counter()
    py_set: dict[str, list[str]] = {}
    table = ""
    array: str | None = None
    for line in text.splitlines():
        m = _TABLE_RE.match(line)
        if m:
            table, array = m.group(1).strip("\"'"), None
            skeleton.append(line)
            continue
        if array is None:
            member = next(
                (
                    (key, mm)
                    for (tbl, key), rx in _PY_SET_MEMBERS.items()
                    if tbl == table and (mm := rx.match(line))
                ),
                None,
            )
            if member is not None:
                key, mm = member
                py_set.setdefault(f"{table}/{key}", []).append(f"{mm.group(1)}.{mm.group(2)}")
                skeleton.append(f"<python-version {table}/{key}>")
                continue
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
    return skeleton, names, py_set


def check_pyproject(base: str, head: str, image: str | None = None) -> list[str]:
    """Return the refusal reasons (empty = pin-only). `image` = the head Dockerfile's Python X.Y."""
    base_skel, base_names, base_py = _pyproject_reduce(base)
    head_skel, head_names, head_py = _pyproject_reduce(head)
    reasons = _skeleton_diff(base_skel, head_skel)
    reasons += _set_move_reasons(base_py, head_py, image)
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
_UV_WHEEL_LINE_RE = _UV_PIN_LINE_RES[3]
_UV_REQUIRES_PYTHON_RE = re.compile(r'^requires-python = ">=(\d+)\.(\d+)"$')
_UV_PY_MARKER_RE = re.compile(
    r"^    \"python_full_version (?:>=|<=|==|!=|<|>) '\d+\.\d+(?:\.\d+)?'\",$"
)
# `{ name = "kopf", specifier = ">=1.37" },` — keep the name, normalise the constraint.
_UV_SPECIFIER_RE = re.compile(r"(?<=[ ,]specifier = )\"[^\"]*\"")


def _closing(lines: list[str], i: int, entry: re.Pattern[str]) -> int | None:
    """Index of the `]` closing the array opened at lines[i] when every line between matches
    `entry`; None otherwise (then the array is structure, line by line)."""
    j = i + 1
    while j < len(lines) and entry.match(lines[j]):
        j += 1
    return j if j < len(lines) and lines[j] == "]" else None


def _uv_lock_reduce(text: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Return (skeleton, the header's Python version set, the header's python_full_version
    resolution markers). Pin lines collapse to ONE `<pin>` marker per run — a new release ships a
    different NUMBER of wheels (zero included), and the wheel count is pin material."""
    skeleton: list[str] = []
    py_set: dict[str, list[str]] = {}
    markers: list[str] = []
    lines = text.splitlines()
    header = True  # before the first [[package]]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("[[package]]"):
            header = False
        if header and (m := _UV_REQUIRES_PYTHON_RE.match(line)):
            py_set.setdefault("uv.lock/requires-python", []).append(f"{m.group(1)}.{m.group(2)}")
            skeleton.append("<python-version uv.lock/requires-python>")
            i += 1
            continue
        if header and line == "resolution-markers = [":
            end = _closing(lines, i, _UV_PY_MARKER_RE)
            if end is not None:
                markers.extend(lines[i + 1 : end])
                i = end + 1
                continue
        if line == "wheels = [" and (end := _closing(lines, i, _UV_WHEEL_LINE_RE)) is not None:
            if not skeleton or skeleton[-1] != "<pin>":
                skeleton.append("<pin>")
            i = end + 1
            continue
        if any(r.match(line) for r in _UV_PIN_LINE_RES):
            if not skeleton or skeleton[-1] != "<pin>":
                skeleton.append("<pin>")
            i += 1
            continue
        skeleton.append(_UV_SPECIFIER_RE.sub('"<specifier>"', line))
        i += 1
    return skeleton, py_set, markers


def check_uv_lock(base: str, head: str, image: str | None = None) -> list[str]:
    """Return the refusal reasons (empty = pin-only). `image` = the head Dockerfile's Python X.Y."""
    base_skel, base_py, base_markers = _uv_lock_reduce(base)
    head_skel, head_py, head_markers = _uv_lock_reduce(head)
    reasons = _skeleton_diff(base_skel, head_skel)
    set_reasons = _set_move_reasons(base_py, head_py, image)
    reasons += set_reasons
    if base_markers != head_markers and (base_py == head_py or set_reasons):
        reasons.append(
            "python resolution-markers changed without an admitted requires-python move: "
            f"{base_markers} → {head_markers}"
        )
    return reasons


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
    if len(argv) not in (4, 5) or argv[1] not in ("pyproject", "uv-lock"):
        print(__doc__, file=sys.stderr)
        return 2
    kind, base_path, head_path = argv[1], Path(argv[2]), Path(argv[3])
    base = base_path.read_text(encoding="utf-8") if base_path.exists() else ""
    head = head_path.read_text(encoding="utf-8") if head_path.exists() else ""
    docker_path = Path(argv[4]) if len(argv) == 5 else None
    dockerfile = (
        docker_path.read_text(encoding="utf-8") if docker_path and docker_path.exists() else None
    )
    image = image_python_version(dockerfile)
    check = check_pyproject if kind == "pyproject" else check_uv_lock
    reasons = check(base, head, image)
    if reasons:
        for r in reasons[:20]:
            print(r, file=sys.stderr)
        if len(reasons) > 20:
            print(f"… and {len(reasons) - 20} more", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
