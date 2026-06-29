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

### Standing project key (the funding ceiling)

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

### Ephemeral session key (the per-session breaker)

A standing key's `resetInterval` is a *soft per-window* cap — one runaway session can eat the whole
window. Mint a fresh **single-shot** key per agent dispatch instead: a HARD `budgetUSD` cap with **no
reset window** (`limit_reset=null`) plus a self-destruct `expiresAt`. The coordinator stamps the
pre-flight cost estimate into `budgetUSD` (see homelab `agents/estimate_budget.py`) and a unique
`session`, then deletes the CR (or lets `expiresAt` expire it) when the pod finishes.

```yaml
apiVersion: openrouter.teststuff.net/v1alpha1
kind: OpenRouterKey
metadata: { name: sleep-tracking-issue-42-r1, namespace: sleep-tracking }
spec:
  project: sleep-tracking
  budgetUSD: 0.50            # pre-flight estimate × buffer — a HARD cap (OpenRouter 403s past it)
  ephemeral: true
  session: issue-42-round-1  # required: makes the key + Secret unique per session
  expiresAt: "2026-06-29T14:00:00Z"  # OpenRouter auto-expires the key
```
Writes to `sleep-tracking-session-issue-42-round-1-openrouter` (not the shared standing Secret), so
concurrent sessions never collide. Override the target with `secretName:` if the pod expects a fixed
name.
