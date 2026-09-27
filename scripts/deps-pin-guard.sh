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
#     its code stays possible; nothing is un-gated);
#   - it touches ONLY guarded files → every guarded file's delta must be pin material;
#   - it touches guarded files + other UN-OWNED paths (.pre-commit-config.yaml, a workflow pin) →
#     refused: those lanes have their own owners and a shared PR would let one lane's change ride
#     the other's gate.
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
# Ownership is read from CODEOWNERS at the PR's merge-base (what GitHub evaluates): a pattern with
# no owner un-owns; the repo must carry an owning `*` catch-all (otherwise "not un-owned" does not
# imply "owned" and every mixed PR is refused, the pre-2026-09-27 behaviour). Only the simple
# carve-out shapes are understood — `/file` (exact) and `/dir/` (prefix); any other ownerless
# pattern makes the guard refuse mixed PRs rather than guess (fail closed).
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
  # Classify the other paths against CODEOWNERS at the merge-base (GitHub's own evaluation point).
  content_at "$BASE_SHA" CODEOWNERS "$tmp/codeowners"
  rules=$(grep -Ev '^\s*(#|$)' "$tmp/codeowners" || true)
  star_owned=$(printf '%s\n' "$rules" | grep -Ec '^\*\s+\S' || true)
  unowned=$(printf '%s\n' "$rules" | awk 'NF==1 {print $1}')
  owned_extra=""; unowned_extra=""; unparseable=""
  for f in $extra; do
    hit=""
    for pat in $unowned; do
      case "$pat" in
        */) p="${pat#/}"; case "$f" in "$p"*) hit=1;; esac;;
        /*) [ "$f" = "${pat#/}" ] && hit=1;;
        */*) case "$f" in "$pat"|"$pat"/*) hit=1;; esac;;
        *) unparseable="$unparseable $pat";;
      esac
    done
    if [ -n "$hit" ]; then unowned_extra="$unowned_extra $f"; else owned_extra="$owned_extra $f"; fi
  done
  if [ -n "$unparseable" ]; then
    echo "deps-pin-guard: FAIL — cannot classify the PR's other paths: CODEOWNERS carve-out pattern(s) I do not understand:$unparseable" >&2
    echo "only '/file' and '/dir/' ownerless patterns are recognised; a mixed PR cannot be admitted without knowing what is owned" >&2
    exit 1
  fi
  if [ "$star_owned" -eq 0 ]; then
    echo "deps-pin-guard: FAIL — PR touches guarded dep file(s) ($touched ) AND other paths, and CODEOWNERS has no owning '*' catch-all:" >&2
    printf '  %s\n' $extra >&2
    echo "without a catch-all 'not un-owned' does not mean 'owned'; a dep bump must be a pure {$(echo $GUARD_SET | tr ' ' ',')} diff" >&2
    exit 1
  fi
  if [ -n "$unowned_extra" ]; then
    echo "deps-pin-guard: FAIL — PR touches guarded dep file(s) ($touched ) AND other UN-OWNED paths:" >&2
    printf '  %s\n' $unowned_extra >&2
    echo "each carved-out lane rides its own gate; a dep bump must be a pure {$(echo $GUARD_SET | tr ' ' ',')} diff" >&2
    exit 1
  fi
  echo "deps-pin-guard: pass — PR also touches OWNED path(s) ($owned_extra ); the code owner's required review covers the dep-file change ($touched )"
  exit 0
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
