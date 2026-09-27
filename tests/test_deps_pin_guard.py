"""deps-pin-guard — the CODEOWNERS replacement owner for devbox.{json,lock} + pyproject.toml +
uv.lock (scripts/deps-pin-guard.sh + scripts/deps_pin_shape.py).

Each case builds a tiny git repo (base commit → head commit) and runs the REAL script in its
`--local <base> <head>` mode, so the fileset + content path, the shape tests and the exit/message
contract are all exercised end to end. Expected verdicts come from the contract in the two scripts'
header comments, not from running the code: a guarded file changes only under a human code-owner
review (the PR also touches an OWNED path) or with a pin-only shape — pin material = version
constraints / resolver hashes of EXISTING packages; everything else (new/removed package,
requires-python, tool tables, a new [[package]]) is structure → refused, exit 1,
`deps-pin-guard: FAIL`; guarded + another UN-owned lane's file → refused.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_GUARD = _REPO / "scripts" / "deps-pin-guard.sh"

PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "kopf>=1.37",
    "pydantic>=2",
    "kubernetes>=29",
]

[project.optional-dependencies]
sdk = ["openrouter"]

[dependency-groups]
dev = [
    "pytest",
    "ruff",
    "pyyaml>=6.0.3",
]

[tool.ruff]
line-length = 100

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

# Real uv.lock shapes with shortened URLs (the checker tests the line SHAPE, not its length).
_SDIST_140 = (
    'sdist = { url = "https://f.example/aa/aiosignal-1.4.0.tar.gz", '
    'hash = "sha256:aaaa", size = 25007 }'
)
_WHEEL_140 = (
    '    { url = "https://f.example/aa/aiosignal-1.4.0-py3-none-any.whl", '
    'hash = "sha256:bbbb", size = 7490 },'
)

UV_LOCK = f"""\
version = 1
revision = 3
requires-python = ">=3.14"

[[package]]
name = "aiosignal"
version = "1.4.0"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [
    {{ name = "frozenlist" }},
]
{_SDIST_140}
wheels = [
{_WHEEL_140}
]

[[package]]
name = "demo"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "aiosignal" }},
]

[package.dev-dependencies]
dev = [
    {{ name = "pytest" }},
]

[package.metadata]
requires-dist = [
    {{ name = "aiosignal", specifier = ">=1.4" }},
]

[package.metadata.requires-dev]
dev = [{{ name = "pytest" }}]
"""

DEVBOX = """\
{
  "packages": [
    "python@3.14",
    "uv@latest"
  ],
  "shell": {
    "scripts": {
      "ci": ["bash scripts/ci.sh"]
    }
  }
}
"""

_DEVBOX_LOCK = '{"lockfile_version": "1", "packages": {"uv@latest": {"version": "%s"}}}\n'

# The repo's ownership shape: an owning `*` catch-all with ownerless carve-outs (LAST match wins).
CODEOWNERS = """\
* @owner
/devbox.json
/devbox.lock
/pyproject.toml
/uv.lock
/.pre-commit-config.yaml
/.github/workflows/
/CODEOWNERS @owner
"""

BASE_FILES = {
    "CODEOWNERS": CODEOWNERS,
    "pyproject.toml": PYPROJECT,
    "uv.lock": UV_LOCK,
    "devbox.json": DEVBOX,
    "devbox.lock": _DEVBOX_LOCK % "0.9.0",
    ".pre-commit-config.yaml": "repos: []\n",
    ".github/workflows/ci.yaml": "on: push\n",
    "src/demo.py": "X = 1\n",
}


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    ).stdout.strip()


def _commit_all(repo: pathlib.Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: pathlib.Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _write(repo, BASE_FILES)
    return repo, _commit_all(repo, "base")


def _run_guard(repo: pathlib.Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_GUARD), "--local", base, head],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _verdict(
    tmp_path: pathlib.Path, head_files: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    repo, base = _make_repo(tmp_path)
    _write(repo, head_files)
    head = _commit_all(repo, "head")
    return _run_guard(repo, base, head)


# ── admitted: pin material only ─────────────────────────────────────────────────────────────────


def test_pyproject_constraint_bump_is_admitted(tmp_path: pathlib.Path) -> None:
    # Renovate pep621: same package, only the constraint moves — the lane's whole purpose.
    r = _verdict(tmp_path, {"pyproject.toml": PYPROJECT.replace('"kopf>=1.37"', '"kopf>=1.38"')})
    assert r.returncode == 0, r.stderr
    assert "deps-pin-guard: pass" in r.stdout


def test_pyproject_dev_group_and_build_requires_bumps_are_admitted(tmp_path: pathlib.Path) -> None:
    # Multi-line (dependency-groups) and SINGLE-LINE arrays (build-system requires, the optional
    # extra) are both dep arrays — this repo writes `requires = ["hatchling"]` inline.
    head = (
        PYPROJECT.replace('"pyyaml>=6.0.3"', '"pyyaml>=6.1"')
        .replace('requires = ["hatchling"]', 'requires = ["hatchling>=1.27"]')
        .replace('sdk = ["openrouter"]', 'sdk = ["openrouter>=0.3"]')
    )
    r = _verdict(tmp_path, {"pyproject.toml": head})
    assert r.returncode == 0, r.stderr


def test_pyproject_inline_array_new_package_is_refused(tmp_path: pathlib.Path) -> None:
    head = PYPROJECT.replace('sdk = ["openrouter"]', 'sdk = ["openrouter", "httpx"]')
    r = _verdict(tmp_path, {"pyproject.toml": head})
    assert r.returncode == 1
    assert "new dependency: project.optional-dependencies/sdk/httpx" in r.stderr


def test_uv_lock_version_hash_bump_is_admitted(tmp_path: pathlib.Path) -> None:
    # A uv lock bump: version + sdist + wheels change; a new release may ship MORE wheels and carry
    # the optional upload-time (the wheel count is pin material, not structure); the requires-dist
    # specifier follows the pyproject constraint.
    sdist_150 = (
        'sdist = { url = "https://f.example/aa/aiosignal-1.5.0.tar.gz", hash = "sha256:cccc", '
        'size = 25100, upload-time = "2026-09-03T22:54:43.528Z" }'
    )
    wheels_150 = (
        '    { url = "https://f.example/aa/aiosignal-1.5.0-py3-none-any.whl", '
        'hash = "sha256:dddd", size = 7500, upload-time = "2026-09-03T22:54:42.156Z" },\n'
        '    { url = "https://f.example/aa/aiosignal-1.5.0-cp314-cp314-win_amd64.whl", '
        'hash = "sha256:eeee", size = 9000, upload-time = "2026-09-03T22:54:43.156Z" },'
    )
    head_lock = (
        UV_LOCK.replace('version = "1.4.0"', 'version = "1.5.0"')
        .replace(_SDIST_140, sdist_150)
        .replace(_WHEEL_140, wheels_150)
        .replace('specifier = ">=1.4"', 'specifier = ">=1.5"')
    )
    head_py = PYPROJECT.replace('"kopf>=1.37"', '"kopf>=1.38"')
    r = _verdict(tmp_path, {"uv.lock": head_lock, "pyproject.toml": head_py})
    assert r.returncode == 0, r.stderr
    assert "pyproject.toml" in r.stdout and "uv.lock" in r.stdout


def test_uv_lock_revision_only_bump_is_admitted(tmp_path: pathlib.Path) -> None:
    r = _verdict(tmp_path, {"uv.lock": UV_LOCK.replace("revision = 3", "revision = 4")})
    assert r.returncode == 0, r.stderr


def test_devbox_version_bump_still_admitted(tmp_path: pathlib.Path) -> None:
    # The pre-existing devbox lane is unchanged by the extension: version strings + the lock.
    r = _verdict(
        tmp_path,
        {
            "devbox.json": DEVBOX.replace('"python@3.14"', '"python@3.15"'),
            "devbox.lock": _DEVBOX_LOCK % "0.9.1",
        },
    )
    assert r.returncode == 0, r.stderr


def test_untouched_guard_set_passes(tmp_path: pathlib.Path) -> None:
    r = _verdict(tmp_path, {"src/demo.py": "X = 2\n"})
    assert r.returncode == 0, r.stderr
    assert "no guarded dep file touched" in r.stdout


# ── refused: structure changed ──────────────────────────────────────────────────────────────────


def test_pyproject_new_package_is_refused(tmp_path: pathlib.Path) -> None:
    head = PYPROJECT.replace(
        '    "kubernetes>=29",\n', '    "kubernetes>=29",\n    "httpx>=0.28",\n'
    )
    r = _verdict(tmp_path, {"pyproject.toml": head})
    assert r.returncode == 1
    assert "deps-pin-guard: FAIL" in r.stderr
    assert "new dependency: project/dependencies/httpx" in r.stderr


def test_pyproject_swapped_package_same_slot_is_refused(tmp_path: pathlib.Path) -> None:
    # Same line count, same position — only the multiset of names tells it apart from a bump.
    r = _verdict(tmp_path, {"pyproject.toml": PYPROJECT.replace('"ruff"', '"black"')})
    assert r.returncode == 1
    assert "new dependency: dependency-groups/dev/black" in r.stderr
    assert "removed dependency: dependency-groups/dev/ruff" in r.stderr


def test_pyproject_requires_python_is_refused(tmp_path: pathlib.Path) -> None:
    # A policy change, not a pin (operator 2026-09-27) — even though it is a version string.
    r = _verdict(tmp_path, {"pyproject.toml": PYPROJECT.replace('">=3.14"', '">=3.15"')})
    assert r.returncode == 1
    assert "requires-python" in r.stderr


def test_pyproject_tool_table_edit_is_refused(tmp_path: pathlib.Path) -> None:
    head = PYPROJECT.replace("line-length = 100", "line-length = 120")
    r = _verdict(tmp_path, {"pyproject.toml": head})
    assert r.returncode == 1
    assert "line-length" in r.stderr


def test_mixed_pin_bump_and_unowned_lane_file_is_refused(tmp_path: pathlib.Path) -> None:
    # Guarded file + another carved-out (un-owned) path: no human reads it and the other lane's
    # gate is not this one → refused. Both the exact-file and the directory carve-out shapes.
    head_py = PYPROJECT.replace('"kopf>=1.37"', '"kopf>=1.38"')
    r = _verdict(
        tmp_path / "a", {"pyproject.toml": head_py, ".pre-commit-config.yaml": "repos: [x]\n"}
    )
    assert r.returncode == 1
    assert "UN-OWNED paths" in r.stderr and ".pre-commit-config.yaml" in r.stderr
    r = _verdict(
        tmp_path / "b", {"uv.lock": UV_LOCK + "\n", ".github/workflows/ci.yaml": "on: pr\n"}
    )
    assert r.returncode == 1
    assert "UN-OWNED paths" in r.stderr and ".github/workflows/ci.yaml" in r.stderr


def test_mixed_with_owned_path_is_admitted_to_the_human_gate(tmp_path: pathlib.Path) -> None:
    # A feature PR adding a dependency WITH its code: src/ is owned by `*`, so GitHub requires the
    # code owner on the whole PR and that read covers pyproject.toml — the guard steps aside.
    # (The pre-2026-09-27 rule "may touch nothing else" made such a PR unmergeable.)
    head_py = PYPROJECT.replace(
        '    "kubernetes>=29",\n', '    "kubernetes>=29",\n    "httpx>=0.28",\n'
    )
    r = _verdict(tmp_path, {"pyproject.toml": head_py, "src/demo.py": "import httpx\n"})
    assert r.returncode == 0, r.stderr
    assert "OWNED path(s)" in r.stdout and "src/demo.py" in r.stdout


def test_mixed_without_star_catch_all_is_refused(tmp_path: pathlib.Path) -> None:
    # No `*` owner → "not un-owned" does not imply "owned" → the old strict rule applies.
    repo, _ = _make_repo(tmp_path)
    _write(repo, {"CODEOWNERS": "/CODEOWNERS @owner\n/pyproject.toml\n"})
    base = _commit_all(repo, "no catch-all")
    _write(
        repo,
        {
            "pyproject.toml": PYPROJECT.replace('"kopf>=1.37"', '"kopf>=1.38"'),
            "src/demo.py": "X = 2\n",
        },
    )
    r = _run_guard(repo, base, _commit_all(repo, "head"))
    assert r.returncode == 1
    assert "no owning '*' catch-all" in r.stderr and "src/demo.py" in r.stderr


def test_mixed_with_unparseable_carve_out_is_refused(tmp_path: pathlib.Path) -> None:
    # An ownerless pattern the guard cannot classify (`*.md` — could un-own anything) → refuse
    # rather than guess which paths are owned.
    repo, _ = _make_repo(tmp_path)
    _write(repo, {"CODEOWNERS": CODEOWNERS + "*.md\n"})
    base = _commit_all(repo, "fancy carve-out")
    _write(
        repo,
        {
            "pyproject.toml": PYPROJECT.replace('"kopf>=1.37"', '"kopf>=1.38"'),
            "src/demo.py": "X = 2\n",
        },
    )
    r = _run_guard(repo, base, _commit_all(repo, "head"))
    assert r.returncode == 1
    assert "do not understand" in r.stderr and "*.md" in r.stderr


def test_missing_codeowners_keeps_the_strict_rule(tmp_path: pathlib.Path) -> None:
    repo, _ = _make_repo(tmp_path)
    (repo / "CODEOWNERS").unlink()
    base = _commit_all(repo, "no codeowners")
    _write(
        repo,
        {
            "pyproject.toml": PYPROJECT.replace('"kopf>=1.37"', '"kopf>=1.38"'),
            "src/demo.py": "X = 2\n",
        },
    )
    r = _run_guard(repo, base, _commit_all(repo, "head"))
    assert r.returncode == 1
    assert "no owning '*' catch-all" in r.stderr


def test_uv_lock_new_package_is_refused(tmp_path: pathlib.Path) -> None:
    # The STRICT rule: a [[package]] uv added for a bump (a new transitive dep) parks for the human.
    new_block = (
        "[[package]]\n"
        'name = "frozenlist"\n'
        'version = "1.8.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://f.example/ff/frozenlist-1.8.0.tar.gz", '
        'hash = "sha256:ffff", size = 1 }\n'
        "wheels = [\n"
        '    { url = "https://f.example/ff/frozenlist-1.8.0-py3-none-any.whl", '
        'hash = "sha256:f0f0", size = 2 },\n'
        "]\n\n"
    )
    head = UV_LOCK.replace('[[package]]\nname = "demo"', new_block + '[[package]]\nname = "demo"')
    r = _verdict(tmp_path, {"uv.lock": head})
    assert r.returncode == 1
    assert 'name = "frozenlist"' in r.stderr


def test_uv_lock_dependency_graph_edit_is_refused(tmp_path: pathlib.Path) -> None:
    head = UV_LOCK.replace(
        '    { name = "frozenlist" },\n',
        '    { name = "frozenlist" },\n    { name = "attrs" },\n',
    )
    r = _verdict(tmp_path, {"uv.lock": head})
    assert r.returncode == 1
    assert 'name = "attrs"' in r.stderr


def test_uv_lock_requires_dist_new_name_is_refused(tmp_path: pathlib.Path) -> None:
    head = UV_LOCK.replace(
        '    { name = "aiosignal", specifier = ">=1.4" },\n',
        '    { name = "aiosignal", specifier = ">=1.4" },\n'
        '    { name = "httpx", specifier = ">=0.28" },\n',
    )
    r = _verdict(tmp_path, {"uv.lock": head})
    assert r.returncode == 1
    assert 'name = "httpx"' in r.stderr


def test_uv_lock_source_change_is_refused(tmp_path: pathlib.Path) -> None:
    head = UV_LOCK.replace(
        'source = { registry = "https://pypi.org/simple" }',
        'source = { registry = "https://evil.example/simple" }',
    )
    r = _verdict(tmp_path, {"uv.lock": head})
    assert r.returncode == 1
    assert "evil.example" in r.stderr


def test_devbox_non_version_edit_still_refused(tmp_path: pathlib.Path) -> None:
    head = DEVBOX.replace('"ci": ["bash scripts/ci.sh"]', '"ci": ["true"]')
    r = _verdict(tmp_path, {"devbox.json": head})
    assert r.returncode == 1
    assert "non-version lines" in r.stderr


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock"])
def test_deleting_a_guarded_file_is_refused(tmp_path: pathlib.Path, path: str) -> None:
    repo, base = _make_repo(tmp_path)
    (repo / path).unlink()
    head = _commit_all(repo, "rm")
    r = _run_guard(repo, base, head)
    assert r.returncode == 1
    assert "deps-pin-guard: FAIL" in r.stderr
