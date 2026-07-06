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
