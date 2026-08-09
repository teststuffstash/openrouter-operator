# Reviewer rubric — openrouter-operator

Project-specific criteria appended to the generic reviewer. You run as a *different* model than the one
that wrote the PR (no shared blind spots). Two PR kinds land here — a **code-fix PR** (below) and a
**dependency/toolchain bump** (the section at the end). Judge the diff; do not rewrite it. Be terse and
line-anchored.

## Code-fix PRs

This is a **kopf operator with a pure core** (`reconcile.decide(desired, observed) -> Plan`) behind a narrow
`OpenRouterPort` Protocol — so bugs are tested offline with a FAKE port, as a decision table. Approve ONLY
if all hold — otherwise `--request-changes` with specific comments:

1. **Regression case.** The diff adds a `decide()` decision-table case that *fails on the old code and passes
   on the new*, encoding the reported bug (right desired/observed inputs, expected `Plan`). Reject if the
   "test" could not have caught the bug it claims to fix.
2. **Tested at the seam, not the I/O boundary.** Logic changes live in `reconcile.py`/`models.py`/`ports.py`
   and are covered; `adapter.py`/`k8s.py`/`operator.py` are the I/O boundary — no live OpenRouter API or real
   cluster in tests (fake the port). Reject a test that needs a real key/network.
3. **`mypy --strict` stays clean.** The typed core is the shift-left gate (a structural SDK change should
   surface here, not at reconcile-time in prod) — no new `# type: ignore` / `Any` widening without reason.
4. **No duplicate tests / no blobs.** New cases are rows/params in the table, not copy-pasted functions; no
   committed binary fixtures.
5. **Scope — keyed on the PR's lane** (the branch prefix names the recipe class):
   - `fix/…` (bug fixes, `.agents/fix.yaml`): touches `src/` + `tests/` only — `chart/` is forbidden.
   - `build/…` (chart deliverables, `.agents/build.yaml`, since #27): touches `chart/` + `tests/`
     only — `src/` is forbidden, and any PrometheusRule alert must be severity `warning` (never
     `info`), symptom-described, with no alert shipped whose metric has no series behind it.
   - Both: minimal diff; `deploy/`, `.github/`, `.agents/`, secrets are forbidden for every lane.

CI (`devbox run ci` = ruff + mypy --strict + pytest, `fail_under`) runs separately — don't re-litigate what
a status check covers; review what it can't.

## Dependency / toolchain bumps (devbox, Renovate)

A PR that only bumps `devbox.lock`/`devbox.json`/a lockfile is not a code-fix — the decision-table criteria
don't apply. Follow the generic reviewer's **migration investigation**: for a MAJOR bump (label `major`),
read the tool's upstream breaking-changes and check *our* usage (`scripts/`, `.github/`, `chart/`, `deploy/`,
`Dockerfile`, the SDK adapter). A `major` bump is human-gated (not auto-merged) — document what must change so
a human can merge with confidence once CI is green.
