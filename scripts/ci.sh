#!/usr/bin/env bash
# CI gate — the seam. The same command runs locally and in CI (`devbox run ci`); the logic + tool
# versions live here / in devbox.json, not in the workflow YAML. `mypy --strict` is the shift-left
# gate: a breaking OpenRouter API change (after bumping the SDK) fails here, before the cluster.
set -euo pipefail
export UV_LINK_MODE=copy # the jail's /nix bind-mount can't hardlink; copy avoids a noisy warning

echo "==> uv sync (frozen)"
# --frozen: install exactly what uv.lock pins and FAIL if pyproject.toml has drifted from it,
# rather than silently re-resolving and rewriting the committed lock mid-CI. A committed lock
# whose CI does not enforce it is not a lock. Matches oracle-fleet and sleep-tracking; the
# contract is homelab docs/patterns/python-stack.md ("What a Python stack must do", item 3).
uv sync --frozen

echo "==> ruff check"
uv run ruff check

echo "==> ruff format --check"
uv run ruff format --check

echo "==> mypy --strict"
uv run mypy

echo "==> pytest + coverage"
uv run pytest --cov
