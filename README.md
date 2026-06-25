# openrouter-operator

A small Kubernetes operator ([kopf](https://github.com/nolar/kopf)) that manages **OpenRouter API
keys, budgets, and guardrails as custom resources** — so per-project keys, weekly spend caps, and
model guardrails live in git and reconcile in-cluster, not in the OpenRouter UI.

```yaml
apiVersion: openrouter.teststuff.net/v1alpha1
kind: OpenRouterKey
metadata:
  name: sleep-tracking
  namespace: sleep-tracking
spec:
  project: sleep-tracking
  budgetUSD: 5         # spend cap per window
  resetInterval: weekly  # weekly by default — caps a runaway agent's blast radius
  guardrail: only-free
  # secretName: defaults to <project>-openrouter
```

The operator mints/maintains the key on OpenRouter and writes the value into a k8s Secret
(`OPENROUTER_API_KEY`); an ESO `PushSecret` then carries it to Infisical (source of truth) and back
out to the consuming pod via an ExternalSecret.

## Why an operator + the official SDK (not provider-http / a TF provider)

This is a deliberate testing choice (see the homelab `docs/agents/` discussion):

- **Smaller, typed test surface.** The reconcile logic talks to a narrow `OpenRouterPort` Protocol,
  never the API directly — so it's tested with a fake, offline, as a **decision table**.
- **Breaking API changes fail at CI, not in the cluster.** The official
  [`openrouter` Python SDK](https://github.com/OpenRouterTeam/python-sdk) is generated from
  OpenRouter's OpenAPI spec; bump it and a structural change surfaces as a `mypy --strict` error in
  CI — the shift-left gate — instead of a silently-wrong request failing at reconcile in prod.
- **No live test API needed.** Mock the port; the SDK lives behind one adapter.

## Layout

| Module | Role | Strictly tested? |
|---|---|---|
| `models.py` | the CR spec (Pydantic, validated) | ✅ |
| `ports.py` | the `OpenRouterPort` Protocol + value types | ✅ |
| `reconcile.py` | `decide(desired, observed) -> Plan` — pure, the decision table | ✅ |
| `adapter.py` | the Protocol via the `openrouter` SDK | I/O boundary |
| `k8s.py` | write the key into a Secret | I/O boundary |
| `operator.py` | kopf handlers (parse → observe → decide → apply) | I/O boundary |

## Develop

```bash
devbox run ci          # uv sync + ruff + mypy --strict + pytest (the seam — same as CI)
devbox run scan-secrets
```

To run the operator for real you also need the SDK + a management key:

```bash
uv sync --extra sdk
OPENROUTER_MANAGEMENT_KEY=sk-or-... kopf run -m openrouter_operator.operator
```

## Status

Early scaffold. The reconcile logic + CRD + decision-table tests are complete and CI-green. The
`adapter.py` SDK calls are best-effort against the **beta** SDK — verify the exact
`api_keys.{create,get,update,delete}` signatures against the installed package and tighten (then
drop the `openrouter.*` mypy override in `pyproject.toml` for full compile-time checking of the
adapter too).
