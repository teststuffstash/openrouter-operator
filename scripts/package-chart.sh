#!/usr/bin/env bash
# Package the openrouter-operator Helm chart and push it to ghcr.io as an OCI artifact.
# chart version AND appVersion are both set to $VERSION, so chart : image : commit move in lockstep
# (the chart's appVersion is the image tag the Deployment resolves). Mirrors sleep-tracking's
# package-chart.sh. CI: .github/workflows/deploy.yaml. Locally:
#   echo "$GHCR_TOKEN" | helm registry login ghcr.io -u <github-user> --password-stdin
#   VERSION=2026.7.5-gabc devbox run package-chart
set -euo pipefail
VERSION="${VERSION:?set VERSION, e.g. VERSION=2026.7.5-g<sha>}"
CHART_REPO="${CHART_REPO:-oci://ghcr.io/teststuffstash/charts}"

echo "==> helm package openrouter-operator $VERSION (version + appVersion = $VERSION)"
helm package chart/ --version "$VERSION" --app-version "$VERSION"

echo "==> helm push → $CHART_REPO"
helm push "openrouter-operator-${VERSION}.tgz" "$CHART_REPO"
echo "==> pushed $CHART_REPO/openrouter-operator:$VERSION"
