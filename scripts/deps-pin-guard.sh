#!/bin/sh
# deps-pin-guard — the "ownership REPLACED, not dropped" check for the carved-out dep-bump lane
# (homelab pin-only-lint doctrine, 2026-08-11 reviewer-enable retrace).
#
# devbox.json + devbox.lock (2026-08-11) and pyproject.toml + uv.lock (operator direction
# 2026-09-27) are UNOWNED in CODEOWNERS so Renovate / devbox-update bumps keep auto-merging on the
# bot approval under require_code_owner_review=true — this check is the owner that replaced them.
# It guarantees one thing: NO change to a guarded file lands without EITHER a human code-owner
# review OR a pin-only shape. Concretely, for a PR touching a guarded file:
#   - it also touches an OWNED path → GitHub requires the code owner on the whole PR, and that
#     read covers the dep-file change too → pass (a feature PR adding a dependency together with
#     an owned change stays possible; nothing is un-gated);
#   - every other path it touches is UN-owned (src/, the Dockerfile, a carved-out lane file —
#     the repo's shape since the 2026-10-13 narrowing, homelab ADR-154) → every guarded file's
#     delta must be pin material, exactly as for a pure dep-file PR: the other paths ride their
#     own gate (the reviewer + CI, pin-only-lint for a workflow pin) and nothing rides this one.
#     Until 2026-10-13 such a mix was refused outright (openrouter-operator#83's fileset rule) —
#     under the whole-repo `*` owner every non-lane path was owned, so "mixed with un-owned" could
#     only mean two mechanical lanes in one PR; with src/ un-owned it is a worker adapting a
#     Renovate bump (#90: lock refresh + Dockerfile), and refusing it would park every such PR.
# Pin material per file:
#   devbox.json     version-string lines (package@version, "version": fields);
#   devbox.lock     machine-generated hash material — nix verifies it against the substituter
#                   signatures, so it is admitted as a whole;
#   pyproject.toml  version constraints of EXISTING dependencies inside the dependency arrays
#                   (same package names on both sides — a new/removed package, requires-python,
#                   [project] version and every tool table are structure);
#   uv.lock         the resolver's version / sdist / wheel (url, hash, size) lines and the
#                   requires-dist specifiers — the [[package]] set, sources and dependency graph
#                   must be identical (a new transitive package parks for the human: STRICT rule).
# The shape tests live in scripts/deps_pin_shape.py (its docstring is the contract).
#
# Ownership is read from CODEOWNERS at the PR's merge-base (what GitHub evaluates), LAST matching
# pattern wins, a pattern with no owner un-owns, a path no pattern matches is UN-owned (the repo
# has no `*` catch-all since 2026-10-13 — it owns what governs the agent, path by path). Only the
# simple shapes are understood — `*` (everything), `/dir/` (prefix) and `/path` (that file or
# directory); any other pattern makes the guard refuse mixed PRs rather than guess (fail closed).
# A missing CODEOWNERS owns nothing, so a mixed PR gets the pin-shape test.
#
# Runs inside the required `ci` check on pull_request only (a push run has no PR fileset).
# Reads the filenames via the API — the checkout is shallow, a three-dot git diff has no merge
# base here (the homelab ratchet's own founding bug, PR#214). File contents at merge-base and head
# come from `git fetch --depth=1 <sha>` (the pin-only-lint step's mechanism) with the contents API
# as the fallback.
#
# Self-test seam: `deps-pin-guard.sh --local <base> <head>` reads the fileset and contents from the
# git repo in the cwd instead of the API (tests/test_deps_pin_guard.py drives it that way).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
GUARD_SET="${GUARD_SET:-devbox.json devbox.lock pyproject.toml uv.lock}"

if [ "${1:-}" = "--local" ]; then
  MODE=local; BASE_SHA="${2:?base}"; HEAD_SHA="${3:?head}"
else
  MODE=api
  [ "${GITHUB_EVENT_NAME:-}" = "pull_request" ] || { echo "deps-pin-guard: not a PR run — skip"; exit 0; }
  PR="${PR_NUMBER:?}"; REPO="${GITHUB_REPOSITORY:?}"
fi

# content_at <sha> <path> <outfile> — the file at <sha>, empty when it does not exist there.
content_at() {
  : >"$3"
  if [ "$MODE" = local ]; then
    git show "$1:$2" >"$3" 2>/dev/null || : >"$3"
    return 0
  fi
  if git cat-file -e "$1^{commit}" 2>/dev/null \
     || git fetch --no-tags --depth=1 origin "$1" >/dev/null 2>&1; then
    if git show "$1:$2" >"$3" 2>/dev/null; then return 0; fi
    : >"$3"
  fi
  gh api -H "Accept: application/vnd.github.raw+json" \
     "repos/$REPO/contents/$2?ref=$1" >"$3" 2>/dev/null || : >"$3"
}

if [ "$MODE" = local ]; then
  files=$(git diff --name-only "$BASE_SHA" "$HEAD_SHA")
else
  files=$(gh api "repos/$REPO/pulls/$PR/files?per_page=100" --paginate --jq '.[].filename')
fi
touched=""
for g in $GUARD_SET; do
  if printf '%s\n' "$files" | grep -qx "$g"; then touched="$touched $g"; fi
done
[ -n "$touched" ] || { echo "deps-pin-guard: no guarded dep file touched — pass"; exit 0; }

if [ "$MODE" = api ]; then
  HEAD_SHA=$(gh api "repos/$REPO/pulls/$PR" --jq '.head.sha')
  BASE_SHA=$(gh api "repos/$REPO/compare/$(gh api "repos/$REPO/pulls/$PR" --jq '.base.sha')...$HEAD_SHA" \
               --jq '.merge_base_commit.sha')
fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

extra=$(printf '%s\n' "$files" | grep -vxF "$(printf '%s\n' $GUARD_SET)" || true)
if [ -n "$extra" ]; then
  # Classify the other paths against CODEOWNERS at the merge-base (GitHub's own evaluation point):
  # walk the rules in order, the last one that matches decides (owners present → owned).
  content_at "$BASE_SHA" CODEOWNERS "$tmp/codeowners"
  grep -Ev '^[[:space:]]*(#|$)' "$tmp/codeowners" | sed 's/[[:space:]]#.*$//' >"$tmp/rules" || true
  owned_extra=""; unowned_extra=""; unparseable=""
  while read -r pat _; do
    case "$pat" in
      '*') ;;
      *'*'*|*'?'*|*'['*|*'!'*|*'\'*) unparseable="$unparseable $pat";;
      /*) ;;
      *) unparseable="$unparseable $pat";;
    esac
  done <"$tmp/rules"
  if [ -n "$unparseable" ]; then
    echo "deps-pin-guard: FAIL — cannot classify the PR's other paths: CODEOWNERS pattern(s) I do not understand:$unparseable" >&2
    echo "only '*', '/dir/' and '/path' patterns are recognised; a mixed PR cannot be admitted without knowing what is owned" >&2
    exit 1
  fi
  for f in $extra; do
    owned=""
    while read -r pat owners; do
      hit=""
      case "$pat" in
        '*') hit=1;;
        */) p="${pat#/}"; case "$f" in "$p"*) hit=1;; esac;;
        *) p="${pat#/}"; case "$f" in "$p"|"$p"/*) hit=1;; esac;;
      esac
      if [ -n "$hit" ]; then
        if [ -n "$owners" ]; then owned=1; else owned=""; fi
      fi
    done <"$tmp/rules"
    if [ -n "$owned" ]; then owned_extra="$owned_extra $f"; else unowned_extra="$unowned_extra $f"; fi
  done
  if [ -n "$owned_extra" ]; then
    echo "deps-pin-guard: pass — PR also touches OWNED path(s) ($owned_extra ); the code owner's required review covers the dep-file change ($touched )"
    exit 0
  fi
  # Every other path is un-owned: those ride their own gate (reviewer + CI, pin-only-lint); the
  # guarded files still need the pin-only shape below — nothing rides THIS gate (2026-10-13).
  echo "deps-pin-guard: PR also touches UN-OWNED path(s) ($unowned_extra ) — they ride their own gate; the dep-file delta ($touched ) must still be pin-only"
fi

for g in $touched; do
  content_at "$BASE_SHA" "$g" "$tmp/base"
  content_at "$HEAD_SHA" "$g" "$tmp/head"
  case "$g" in
    devbox.lock) continue;;
    devbox.json)
      # delta must be version-material only: package@version strings or "version": fields.
      patch=$(git diff --no-index -- "$tmp/base" "$tmp/head" || true)
      nonpin=$(printf '%s\n' "$patch" | grep -E '^[-+][^-+]' \
                 | grep -Ev '^[-+]\s*"[A-Za-z0-9@/._-]+@[A-Za-z0-9._-]+",?\s*$' \
                 | grep -Ev '^[-+]\s*"version"\s*:' || true)
      if [ -n "$nonpin" ]; then
        echo "deps-pin-guard: FAIL — $g delta carries non-version lines:" >&2
        printf '%s\n' "$nonpin" | head -10 >&2
        exit 1
      fi;;
    pyproject.toml|uv.lock)
      case "$g" in pyproject.toml) kind=pyproject;; *) kind=uv-lock;; esac
      if ! reasons=$(python3 "$HERE/deps_pin_shape.py" "$kind" "$tmp/base" "$tmp/head" 2>&1); then
        echo "deps-pin-guard: FAIL — $g delta is not pin-only (structure changed):" >&2
        printf '%s\n' "$reasons" | head -20 >&2
        echo "only version constraints / resolver hashes of EXISTING packages ride this lane; a new or removed package, requires-python or any other edit takes the owned route" >&2
        exit 1
      fi;;
  esac
done
echo "deps-pin-guard: pass — pure dep-pin diff ($touched )"
