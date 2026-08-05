"""Dependency-declaration assertions — the dev group must list every package that test modules
import directly, so a transitive-vanishing event (kopf/kubernetes dropping pyyaml) can't silently
break CI.

Pattern: same as test_chart_rbac.py — parse the config file, not the installed environment.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO / "pyproject.toml"


def _dev_deps() -> list[str]:
    """Return the raw dependency strings from ``[dependency-groups] dev``."""
    with open(_PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    deps: list[str] = data["dependency-groups"]["dev"]
    return deps


@pytest.mark.parametrize(
    ("package", "import_name"),
    [
        # tests/test_chart_rbac.py imports yaml — pyyaml must be a declared dev dep
        ("pyyaml", "yaml"),
    ],
)
def test_dev_declares_package_for_test_import(package: str, import_name: str) -> None:
    deps = _dev_deps()
    # Match either an exact name or a PEP 508 specifier starting with the package name.
    found = any(
        dep.lower().replace("-", "_").replace(" ", "") == package or dep.lower().startswith(package)
        for dep in deps
    )
    assert found, (
        f"{package!r} (needed by test modules that `import {import_name}`) is not declared in "
        f"[dependency-groups] dev in pyproject.toml. Current dev deps: {deps}"
    )
