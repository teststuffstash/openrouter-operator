#!/usr/bin/env bash
# CI gate — the seam. The same command runs locally and in CI (`devbox run ci`); the logic + tool
# versions live here / in devbox.json, not in the workflow YAML. `mypy --strict` is the shift-left
# gate: a breaking OpenRouter API change (after bumping the SDK) fails here, before the cluster.
set -euo pipefail
export UV_LINK_MODE=copy # the jail's /nix bind-mount can't hardlink; copy avoids a noisy warning

echo "==> uv sync"
uv sync

echo "==> ruff check"
uv run ruff check

echo "==> ruff format --check"
uv run ruff format --check

echo "==> mypy --strict"
uv run mypy

echo "==> pytest + coverage"
uv run pytest --cov
