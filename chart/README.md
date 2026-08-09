# openrouter-operator Helm chart

Packages the operator as one versioned unit — **CRD + RBAC + ExternalSecret (optional) + Deployment**.
Published to `oci://ghcr.io/teststuffstash/charts/openrouter-operator`; chart version == appVersion ==
image tag (a `deploy` sets `--version`/`--app-version` at package time). See the repo
[`README.md`](../README.md) for what the operator *does* and the `OpenRouterKey` CR usage.

## Install

```sh
# 1. provide the management key as a Secret (see below), then:
helm install openrouter-operator oci://ghcr.io/teststuffstash/charts/openrouter-operator \
  --version <chart-version> --namespace openrouter-operator --create-namespace
```

Under ArgoCD, point an `Application` at the OCI chart (`repoURL: ghcr.io/teststuffstash/charts`,
`chart: openrouter-operator`, `targetRevision: <version>`) with `ServerSideApply=true` (the CRD is
large). Multi-source it with your own `values.yaml` for the secret store, à la the homelab platform app.

## The OpenRouter MANAGEMENT key (prerequisite)

The operator mints per-project runtime keys using a **provisioning-capable management key** — you supply
it; the chart never creates it. Getting one is **manual (clickops on OpenRouter)** — there is no API/IaC
to create the first provisioning key:

1. Sign in at <https://openrouter.ai> → **Settings → Provisioning Keys** (a "Provisioning API Key",
   distinct from a normal inference key — it can create/manage *other* keys).
2. **Create Provisioning Key**, copy the `sk-or-...` value (shown once).
3. Store it in your secret backend under the key name you'll reference (default `OPENROUTER_MANAGEMENT_KEY`).
4. Rotate by creating a new provisioning key and updating the backend; the operator picks it up on the
   ExternalSecret refresh (or a pod restart).

## Values

| key | default | meaning |
|---|---|---|
| `image.repository` | `ghcr.io/teststuffstash/openrouter-operator` | operator image |
| `image.tag` | `""` | empty → `.Chart.AppVersion` (the chart version IS the image version) |
| `image.pullPolicy` | `IfNotPresent` | version tags are immutable |
| `managementSecret.create` | `false` | `false`: you provide the Secret; `true`: the chart renders an ExternalSecret |
| `managementSecret.name` | `openrouter-management` | Secret name the Deployment reads |
| `managementSecret.key` | `OPENROUTER_MANAGEMENT_KEY` | key within the Secret |
| `managementSecret.externalSecret.storeRef` | `{name: "", kind: ClusterSecretStore}` | **required when `create:true`** — YOUR store |
| `managementSecret.externalSecret.remoteRef.key` | `""` | name in your store (defaults to `managementSecret.key`) |
| `resources` | 50m/128Mi → 500m/256Mi | container resources |
| `metrics.enabled` | `true` | `/metrics` exporter + its Service, ServiceMonitor and PrometheusRule |
| `metrics.port` | `9090` | exporter port — the container port, Service and `METRICS_PORT` all follow it |
| `metrics.serviceMonitor.enabled` | `true` | set `false` without the prometheus-operator CRDs |
| `metrics.serviceMonitor.interval` / `.scrapeTimeout` | `30s` / `10s` | scrape cadence |
| `metrics.serviceMonitor.additionalLabels` | `{}` | e.g. `{release: kube-prometheus-stack}` when Prometheus selects on a label |
| `metrics.prometheusRule.enabled` | `true` | ship the alert rules |
| `metrics.prometheusRule.additionalLabels` | `{}` | as above, for `ruleSelector` |
| `metrics.prometheusRule.keyOpsPerDay.ceiling` | `1000` | **placeholder** — your account's `keys-modify-api-rpd-v2` limit (see below) |
| `metrics.prometheusRule.keyOpsPerDay.warnAtPercent` | `70` | alert at this fraction of the ceiling |
| `metrics.prometheusRule.keyOpsPerDay.for` | `10m` | how long the threshold must hold |
| `metrics.prometheusRule.accountBalance.burnMultiplier` | `2` | warn at this many days of runway, measured against the trailing-24h burn |
| `metrics.prometheusRule.accountBalance.floorUsd` | `5` | **placeholder** — absolute floor under the self-scaling mark (see below) |
| `metrics.prometheusRule.accountBalance.maxStalenessSeconds` | `3600` | how old the last successful credits poll may be before the alert goes quiet |
| `metrics.prometheusRule.accountBalance.for` | `30m` | how long the threshold must hold |

### Monitoring

The operator exports key-API op counters (`openrouter_key_api_ops_today`, `…_ops_total`,
`…_rate_limited_total`) and the account credit balance (`openrouter_account_credit_usd`,
`…_updated_timestamp_seconds`, `…_poll_failures_total`) on `:9090/metrics`. The chart ships the
Service, ServiceMonitor and two alerts — one per way the account can run out:

- **`OpenRouterKeyOpsDailyBudgetNearlyExhausted`** — the *modify* ops (mint, budget patch, delete)
  issued in the current UTC day pass `warnAtPercent` of the daily ceiling. Past that ceiling every
  key operation is rejected until the next UTC midnight: new `OpenRouterKey` resources stall
  unminted and deleted ones wedge on their finalizers.
- **`OpenRouterAccountCreditNearlyExhausted`** — the credit balance is below `burnMultiplier` days
  of its own trailing-24h burn, or below `floorUsd`, whichever is higher. At zero the keys still
  exist and the CRs still report healthy — only the traffic through them fails, which is why this
  needs an alert of its own rather than showing up in reconcile status.

**Set the ceiling for your account.** OpenRouter does not publish the `keys-modify-api-rpd-v2`
limit and it varies per account, so `1000` is a placeholder. Read the real value from the
`X-RateLimit-Limit` header on a 429 from the key API and set `keyOpsPerDay.ceiling` to it —
otherwise the alert thresholds against the wrong number.

**Set the balance floor for your spend.** The low-water mark scales with what you actually burn,
so it needs no tuning in steady state — but the burn term is `0` on a fresh operator, on an idle
day, and whenever a top-up leaves the 24h trend pointing upward. `floorUsd` is what alerts in
those windows, and `5` is a placeholder: set it to a few days of your own spend. The alert also
goes quiet if the last successful credits poll is older than `maxStalenessSeconds`, since a
reading nobody refreshed is not evidence of a drained account — watch
`openrouter_account_credit_poll_failures_total` for a dead poller.

Both objects are discovered automatically by a kube-prometheus-stack running with
`ruleSelectorNilUsesHelmValues: false` / `serviceMonitorSelectorNilUsesHelmValues: false`; on a
stack that selects by label, use the `additionalLabels` knobs.

### Supplying the key — two ways

**(a) Bring your own Secret** (any tool) — the default:

```sh
kubectl -n openrouter-operator create secret generic openrouter-management \
  --from-literal=OPENROUTER_MANAGEMENT_KEY='sk-or-...'
# values: managementSecret.create stays false
```

**(b) Let the chart render an ExternalSecret** (needs external-secrets.io) — point it at your store:

```yaml
managementSecret:
  create: true
  externalSecret:
    storeRef: { name: my-cluster-store, kind: ClusterSecretStore } # Vault / AWS SM / Infisical / …
    remoteRef: { key: OPENROUTER_MANAGEMENT_KEY }
```
