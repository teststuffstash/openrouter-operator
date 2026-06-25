# Deploy

In-cluster manifests, reconciled by **ArgoCD** from `homelab/argocd/platform/openrouter-operator.yaml`
(the platform app-of-apps points here). Contents:

- `crd.yaml` — the `OpenRouterKey` CustomResourceDefinition
- `rbac.yaml` — ServiceAccount + ClusterRole/Binding (reconcile CRs, write Secrets, kopf housekeeping)
- `externalsecret.yaml` — ESO delivers the OpenRouter **management** key (`OPENROUTER_MANAGEMENT_KEY`)
- `deployment.yaml` — the kopf operator (image from ghcr, non-root, liveness on :8080)

## One-time prereqs (the "required clicks", see homelab `docs/github-setup.md`)

1. **ghcr package public** — make `ghcr.io/teststuffstash/openrouter-operator` public (Package
   settings → Change visibility), else the Deployment `ImagePullBackOff`s. (Public repo → public
   package is simplest; the alternative is an `imagePullSecret`.)
2. **Image built** — the `build-image` workflow (Proxmox VM runner) must have published `:latest`.

## Use

```yaml
apiVersion: openrouter.teststuff.net/v1alpha1
kind: OpenRouterKey
metadata: { name: sleep-tracking, namespace: sleep-tracking }
spec:
  project: sleep-tracking
  budgetUSD: 5
  resetInterval: weekly
  guardrail: only-free
```
The operator mints the key on OpenRouter and writes it to the `sleep-tracking-openrouter` Secret
(`OPENROUTER_API_KEY`).
