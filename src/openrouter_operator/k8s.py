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


def write_key_secret(namespace: str, name: str, key_value: str) -> None:
    """Create-or-replace a Secret holding the OpenRouter key as OPENROUTER_API_KEY."""
    v1 = _core_v1()
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name),
        string_data={"OPENROUTER_API_KEY": key_value},
        type="Opaque",
    )
    try:
        v1.create_namespaced_secret(namespace=namespace, body=body)
    except client.ApiException as exc:
        if exc.status == 409:
            v1.replace_namespaced_secret(name=name, namespace=namespace, body=body)
        else:
            raise
