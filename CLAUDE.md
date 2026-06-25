# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Kubernetes operator (kopf) that manages OpenRouter API keys/budgets/guardrails as custom
resources (`OpenRouterKey`). It exists because OpenRouter-as-IaC via a community Terraform provider
was too buggy and provider-http was untestable without a live API — the official Python SDK gives a
typed, mockable surface so reconcile logic is unit-tested as a decision table and breaking API
changes fail `mypy --strict` in CI, not in the cluster.

## Environment

Running inside a Docker jail — see `/workspace/CLAUDE.md` for container setup, permissions, and tools.

## Tooling & CI

Per-project CLI tools live in `devbox.json` (Nix-backed). The CI gate is the seam:

```bash
devbox run ci          # uv sync + ruff check + ruff format --check + mypy --strict + pytest --cov
devbox run scan-secrets
```

The GitHub Actions workflow just calls `devbox run ci` — keep logic in `scripts/ci.sh`, not YAML.

## Design rules

- **Reconcile logic stays pure and SDK-free.** `reconcile.decide()` depends only on `models` +
  `ports`. Never import the SDK outside `adapter.py`.
- **The port is the test seam.** Add behaviour by extending `OpenRouterPort` + a fake, and assert
  via the decision table in `tests/`. Don't reach for the live API in tests.
- **mypy --strict is the gate.** New code must type-clean. The only relaxations (in `pyproject.toml`)
  are the untyped I/O boundaries (`operator`, `adapter`, `k8s`) and library imports without stubs.
- New CR cases = rows in the decision table, not duplicate test functions.
