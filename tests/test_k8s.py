"""Tests for k8s Secret lifecycle — the ownerReferences fix for issue #43."""

from __future__ import annotations

import logging
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client

from openrouter_operator.k8s import delete_key_secret, write_key_secret


@pytest.fixture
def mock_core_v1() -> Generator[MagicMock, None, None]:
    """Mock the CoreV1Api for testing."""
    with patch("openrouter_operator.k8s._core_v1") as mock_api:
        yield mock_api.return_value


def test_write_key_secret_sets_owner_references(
    mock_core_v1: MagicMock,
) -> None:
    """Secret created for CR should have ownerReferences set for k8s GC."""
    cr_uid = "12345-67890-abcde"
    cr_name = "my-openrouter-key"
    owner_api_version = "openrouter.teststuff.net/v1alpha1"
    owner_kind = "OpenRouterKey"

    write_key_secret(
        namespace="default",
        name="my-secret",
        key_value="sk-or-test",
        key_hash="GK123",
        guardrail="only-free",
        owner_uid=cr_uid,
        owner_name=cr_name,
        owner_api_version=owner_api_version,
        owner_kind=owner_kind,
    )

    # Verify create_namespaced_secret was called
    mock_core_v1.create_namespaced_secret.assert_called_once()
    call_args = mock_core_v1.create_namespaced_secret.call_args

    # Extract the Secret body
    secret_body = call_args.kwargs["body"]
    assert isinstance(secret_body, client.V1Secret)

    # Verify ownerReferences are set
    assert secret_body.metadata.owner_references is not None
    assert len(secret_body.metadata.owner_references) == 1
    owner_ref = secret_body.metadata.owner_references[0]
    assert owner_ref.uid == cr_uid
    assert owner_ref.name == cr_name
    assert owner_ref.kind == owner_kind
    assert owner_ref.api_version == owner_api_version
    assert owner_ref.controller is True
    assert owner_ref.block_owner_deletion is None


def test_write_key_secret_works_without_owner_references(
    mock_core_v1: MagicMock,
) -> None:
    """write_key_secret should work when owner metadata is not provided."""
    write_key_secret(
        namespace="default",
        name="my-secret",
        key_value="sk-or-test",
        key_hash="GK123",
        guardrail="",
    )

    # Verify create_namespaced_secret was called
    mock_core_v1.create_namespaced_secret.assert_called_once()
    call_args = mock_core_v1.create_namespaced_secret.call_args

    # Extract the Secret body
    secret_body = call_args.kwargs["body"]
    assert isinstance(secret_body, client.V1Secret)

    # ownerReferences should be None or empty when not provided
    owner_refs = secret_body.metadata.owner_references
    assert owner_refs is None or len(owner_refs) == 0


def test_write_key_secret_replaces_on_conflict(
    mock_core_v1: MagicMock,
) -> None:
    """write_key_secret replaces existing Secret (409) and preserves ownerReferences."""
    # First call raises 409 (Conflict), second call (replace) succeeds
    mock_core_v1.create_namespaced_secret.side_effect = client.ApiException(status=409)

    cr_uid = "12345-67890-abcde"
    cr_name = "my-openrouter-key"
    owner_api_version = "openrouter.teststuff.net/v1alpha1"
    owner_kind = "OpenRouterKey"

    write_key_secret(
        namespace="default",
        name="my-secret",
        key_value="sk-or-test",
        key_hash="GK123",
        guardrail="",
        owner_uid=cr_uid,
        owner_name=cr_name,
        owner_api_version=owner_api_version,
        owner_kind=owner_kind,
    )

    # Verify that replace_namespaced_secret was called
    mock_core_v1.replace_namespaced_secret.assert_called_once()
    call_args = mock_core_v1.replace_namespaced_secret.call_args

    # Extract the Secret body from replace call
    secret_body = call_args.kwargs["body"]
    assert isinstance(secret_body, client.V1Secret)

    # Verify ownerReferences are still set in the replacement
    assert secret_body.metadata.owner_references is not None
    assert len(secret_body.metadata.owner_references) == 1
    owner_ref = secret_body.metadata.owner_references[0]
    assert owner_ref.uid == cr_uid


def test_delete_key_secret_tolerates_not_found(mock_core_v1: MagicMock) -> None:
    """delete_key_secret should tolerate 404 errors (Secret already gone)."""
    mock_core_v1.delete_namespaced_secret.side_effect = client.ApiException(status=404)

    # Should not raise
    delete_key_secret(namespace="default", name="my-secret")

    mock_core_v1.delete_namespaced_secret.assert_called_once()


def test_delete_key_secret_propagates_other_errors(mock_core_v1: MagicMock) -> None:
    """delete_key_secret should propagate errors that are not 404."""
    mock_core_v1.delete_namespaced_secret.side_effect = client.ApiException(status=500)

    with pytest.raises(client.ApiException) as exc_info:
        delete_key_secret(namespace="default", name="my-secret")

    assert exc_info.value.status == 500


def test_write_key_secret_logs_when_owner_references_skipped(
    mock_core_v1: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """write_key_secret should log a warning when owner metadata is incomplete."""
    caplog.set_level(logging.WARNING)

    # Call without owner metadata (only uid provided, rest empty)
    write_key_secret(
        namespace="default",
        name="my-secret",
        key_value="sk-or-test",
        owner_uid="12345",  # partial owner metadata
    )

    # Verify warning was logged with the exact rendered message
    expected_msg = (
        "Incomplete owner metadata: skipping ownerReferences "
        "(uid=12345, name=, api_version=, kind=)"
    )
    assert any(expected_msg in record.message for record in caplog.records)


def test_write_key_secret_logs_when_owner_name_missing(
    mock_core_v1: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """write_key_secret should log a warning when owner_name is missing."""
    caplog.set_level(logging.WARNING)

    # Call with incomplete owner metadata (missing owner_name)
    write_key_secret(
        namespace="default",
        name="my-secret",
        key_value="sk-or-test",
        owner_uid="12345",
        owner_api_version="openrouter.teststuff.net/v1alpha1",
        owner_kind="OpenRouterKey",
        # owner_name is missing
    )

    # Verify warning was logged with the exact rendered message
    expected_msg = (
        "Incomplete owner metadata: skipping ownerReferences "
        "(uid=12345, name=, api_version=openrouter.teststuff.net/v1alpha1, "
        "kind=OpenRouterKey)"
    )
    assert any(expected_msg in record.message for record in caplog.records)
