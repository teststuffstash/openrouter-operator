#!/usr/bin/env bash
# Open/update the ONE deploy PR in homelab that bumps the openrouter-operator ArgoCD chart pin to
# $VERSION (the chart just published). FIRST-PARTY deploy bump — CI-opened, NOT Renovate (Renovate is
# for external deps only). Fixed branch → exactly one open deploy PR; armed for auto-merge. This is the
# "narrow homelab bump": homelab needs no approval + has no required checks, so an armed PR lands on its
# own (the deploy App opens it; nothing else on homelab auto-merges).
#
# Env: GH_TOKEN (contents + pull_requests write on homelab — a homelab-deploy App token scoped to
#      homelab), VERSION (e.g. 2026.7.5-g<sha12>).
set -euo pipefail
: "${VERSION:?set VERSION}" "${GH_TOKEN:?set GH_TOKEN}"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git clone --quiet --depth 1 "https://x-access-token:${GH_TOKEN}@github.com/teststuffstash/homelab.git" "$WORK/h"
cd "$WORK/h"
APP="argocd/platform/openrouter-operator.yaml"

CUR="$(grep -m1 -E '^[[:space:]]*targetRevision:' "$APP" | awk '{print $2}')"
if [ "$CUR" = "$VERSION" ]; then echo "homelab already pinned to ${VERSION} — nothing to do"; exit 0; fi

git config user.name "homelab-deploy[bot]"
git config user.email "homelab-deploy[bot]@users.noreply.github.com"
git checkout -q -B deploy/openrouter-operator
sed -i -E "0,/^([[:space:]]*)targetRevision:.*/s//\1targetRevision: ${VERSION}/" "$APP"
if git diff --quiet; then echo "no change to ${APP} — nothing to push"; exit 0; fi
git commit -q -am "deploy: openrouter-operator ${VERSION}"
git push -q -f origin deploy/openrouter-operator

export GH_TOKEN
PR="$(gh pr list --repo teststuffstash/homelab --head deploy/openrouter-operator --state open --json number --jq '.[0].number // empty')"
if [ -z "$PR" ]; then
  PR="$(gh pr create --repo teststuffstash/homelab --base master --head deploy/openrouter-operator \
    --title "deploy: openrouter-operator ${VERSION}" \
    --body "First-party chart-version bump (CI-opened, not Renovate). ArgoCD rolls the pinned openrouter-operator chart. Auto-merges (narrow homelab deploy bump)." \
    | grep -oE '[0-9]+$')"
fi
# The MECHANICAL lane (homelab docs/dependency-upgrades.md 2). `automerge` makes homelab's review
# reflex SKIP this PR — a one-line CalVer chart bump is not worth an LLM reviewer session, and
# homelab#104/#105 each burned TWO (dismissed-then-approved on push) before this existed. With
# `dependencies` it also lets the renovate-approve reflex post the mechanical approval homelab's
# require_approval ruleset needs. Applied OUTSIDE the `if` on purpose: `deploy/openrouter-operator`
# is a long-lived branch, so `gh pr create` rarely runs and the reused PR would never get labelled.
gh pr edit "$PR" --repo teststuffstash/homelab --add-label automerge --add-label dependencies \
  || echo "::warning::could not label homelab#${PR} — the review reflex will not skip it"
gh pr merge "$PR" --repo teststuffstash/homelab --auto --squash \
  || echo "::warning::could not arm auto-merge on homelab#${PR} (needs allow_auto_merge on homelab)"
echo "→ homelab deploy PR #${PR} → ${VERSION}"
