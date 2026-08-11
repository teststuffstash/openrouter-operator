#!/bin/sh
# deps-pin-guard — the "ownership REPLACED, not dropped" check for the carved-out dep-bump lane
# (homelab pin-only-lint doctrine, 2026-08-11 reviewer-enable retrace).
#
# devbox.json + devbox.lock are UNOWNED in CODEOWNERS so Renovate / devbox-update bumps keep
# auto-merging on the bot approval once require_code_owner_review=true — this check is the owner
# that replaced them. A PR that touches either file may touch NOTHING else, and its devbox.json
# delta must be version-string lines only; devbox.lock is machine-generated hash material whose
# integrity nix itself verifies against the substituter signatures. Anything wider takes the
# owned-path route and waits for a human.
#
# Runs inside the required `ci` check on pull_request only (a push run has no PR fileset).
# Reads the filenames via the API — the checkout is shallow, a three-dot git diff has no merge
# base here (the homelab ratchet's own founding bug, PR#214).
set -eu
[ "${GITHUB_EVENT_NAME:-}" = "pull_request" ] || { echo "deps-pin-guard: not a PR run — skip"; exit 0; }
PR="${PR_NUMBER:?}"; REPO="${GITHUB_REPOSITORY:?}"
GUARD_SET="${GUARD_SET:-devbox.json devbox.lock}"

files=$(gh api "repos/$REPO/pulls/$PR/files?per_page=100" --paginate --jq '.[].filename')
touched=""
for g in $GUARD_SET; do
  if printf '%s\n' "$files" | grep -qx "$g"; then touched="$touched $g"; fi
done
[ -n "$touched" ] || { echo "deps-pin-guard: no guarded dep file touched — pass"; exit 0; }

extra=$(printf '%s\n' "$files" | grep -vxF "$(printf '%s\n' $GUARD_SET)" || true)
if [ -n "$extra" ]; then
  echo "deps-pin-guard: FAIL — PR touches guarded dep file(s) ($touched ) AND other paths:" >&2
  printf '  %s\n' $extra >&2
  echo "a dep bump must be a pure {devbox.json,devbox.lock} diff; anything wider takes the owned route" >&2
  exit 1
fi

# devbox.json delta must be version-material only: package@version strings or "version": fields.
for g in $GUARD_SET; do
  case "$g" in *.json) ;; *) continue;; esac
  case "$g" in *lock*) continue;; esac
  patch=$(gh api "repos/$REPO/pulls/$PR/files?per_page=100" --paginate \
            --jq ".[] | select(.filename==\"$g\") | .patch // \"\"")
  [ -n "$patch" ] || continue
  nonpin=$(printf '%s\n' "$patch" | grep -E '^[-+][^-+]' \
             | grep -Ev '^[-+]\s*"[A-Za-z0-9@/._-]+@[A-Za-z0-9._-]+",?\s*$' \
             | grep -Ev '^[-+]\s*"version"\s*:' || true)
  if [ -n "$nonpin" ]; then
    echo "deps-pin-guard: FAIL — $g delta carries non-version lines:" >&2
    printf '%s\n' "$nonpin" | head -10 >&2
    exit 1
  fi
done
echo "deps-pin-guard: pass — pure dep-pin diff ($touched )"
