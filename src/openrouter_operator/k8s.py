"""Write the minted key into a k8s Secret. ESO `PushSecret` then carries it to Infisical (the
source of truth) → back out via an ExternalSecret to the consuming pod, keeping the operator out
of the secret-distribution business.
"""

from __future__ import annotations

import logging

from kubernetes import client, config

logger = logging.getLogger(__name__)


def _load_kube_config() -> None:
    """Load in-cluster config, falling back to the local kubeconfig off-cluster (dev)."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _core_v1() -> client.CoreV1Api:
    _load_kube_config()
    return client.CoreV1Api()


SESSION_KEY_LABEL = "openrouter.teststuff.net/session-key"


def write_key_secret(
    namespace: str,
    name: str,
    key_value: str,
    key_hash: str = "",
    guardrail: str = "",
    owner_uid: str = "",
    owner_name: str = "",
    owner_api_version: str = "",
    owner_kind: str = "",
) -> None:
    """Create-or-replace a Secret holding the OpenRouter key as OPENROUTER_API_KEY.

    The label marks it resolvable by the egress proxy's opaque-ref credential injection
    (homelab ADR-087 / FU-018): the proxy honors `ref:<ns>/<name>` ONLY for secrets carrying
    this label, so its get-secret RBAC can never be leveraged into a generic secret oracle.

    When owner metadata is provided, sets ownerReferences so k8s GC deletes the Secret
    when the CR is deleted (issue #43).
    """
    v1 = _core_v1()
    owner_references = None
    if owner_uid and owner_name and owner_api_version and owner_kind:
        owner_references = [
            client.V1OwnerReference(
                api_version=owner_api_version,
                kind=owner_kind,
                name=owner_name,
                uid=owner_uid,
                controller=True,
            )
        ]
    else:
        if any((owner_uid, owner_name, owner_api_version, owner_kind)):
            logger.warning(
                "Incomplete owner metadata: skipping ownerReferences "
                "(uid=%s, name=%s, api_version=%s, kind=%s)",
                owner_uid or "",
                owner_name or "",
                owner_api_version or "",
                owner_kind or "",
            )
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={SESSION_KEY_LABEL: "true"},
            owner_references=owner_references,
        ),
        # KEY_HASH makes post-hoc accounting durable: agent-finalize surfaces it in the run
        # stats, so the ledger-reflex can backfill a cost_unknown run from the management API
        # long after the CR/pod are gone (homelab FU-057 §ledger).
        # GUARDRAIL rides along so the egress proxy can ENFORCE it per request (FU-024): under
        # credential injection the proxy resolves this secret anyway, and a `only-free` key gets
        # its completions rejected for paid models — declared-but-unenforced no longer.
        string_data={"OPENROUTER_API_KEY": key_value, "KEY_HASH": key_hash, "GUARDRAIL": guardrail},
        type="Opaque",
    )
    try:
        v1.create_namespaced_secret(namespace=namespace, body=body)
    except client.ApiException as exc:
        if exc.status == 409:
            v1.replace_namespaced_secret(name=name, namespace=namespace, body=body)
        else:
            raise


def delete_key_secret(namespace: str, name: str) -> None:
    """Delete a session-key Secret, tolerating a 404.

    While newer Secrets have `ownerReferences` set by `write_key_secret` for k8s GC, older orphaned
    Secrets lack them and require explicit deletion. Some are already orphaned from hand-sweeps, so
    a missing Secret is not an error.
    """
    v1 = _core_v1()
    try:
        v1.delete_namespaced_secret(name=name, namespace=namespace)
    except client.ApiException as exc:
        if exc.status != 404:
            raise


def delete_cr(group: str, version: str, plural: str, namespace: str, name: str) -> None:
    """Delete a custom resource (used by the expiry GC timer to collect an expired CR)."""
    _load_kube_config()
    client.CustomObjectsApi().delete_namespaced_custom_object(
        group=group, version=version, namespace=namespace, plural=plural, name=name
    )
