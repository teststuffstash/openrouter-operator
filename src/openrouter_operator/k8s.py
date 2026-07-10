"""Write the minted key into a k8s Secret. ESO `PushSecret` then carries it to Infisical (the
source of truth) → back out via an ExternalSecret to the consuming pod, keeping the operator out
of the secret-distribution business.
"""

from __future__ import annotations

from kubernetes import client, config


def _core_v1() -> client.CoreV1Api:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


SESSION_KEY_LABEL = "openrouter.teststuff.net/session-key"


def write_key_secret(namespace: str, name: str, key_value: str, key_hash: str = "") -> None:
    """Create-or-replace a Secret holding the OpenRouter key as OPENROUTER_API_KEY.

    The label marks it resolvable by the egress proxy's opaque-ref credential injection
    (homelab ADR-087 / FU-018): the proxy honors `ref:<ns>/<name>` ONLY for secrets carrying
    this label, so its get-secret RBAC can never be leveraged into a generic secret oracle.
    """
    v1 = _core_v1()
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, labels={SESSION_KEY_LABEL: "true"}),
        # KEY_HASH makes post-hoc accounting durable: agent-finalize surfaces it in the run
        # stats, so the ledger-reflex can backfill a cost_unknown run from the management API
        # long after the CR/pod are gone (homelab FU-057 §ledger).
        string_data={"OPENROUTER_API_KEY": key_value, "KEY_HASH": key_hash},
        type="Opaque",
    )
    try:
        v1.create_namespaced_secret(namespace=namespace, body=body)
    except client.ApiException as exc:
        if exc.status == 409:
            v1.replace_namespaced_secret(name=name, namespace=namespace, body=body)
        else:
            raise
