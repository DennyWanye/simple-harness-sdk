# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Built-in operation-profile authority for the real file-publish connector.

This is the only current production profile registry.  It deliberately accepts
only an exact :class:`FilePublishConnector` loaded in this process, computes the
implementation digest from that loaded source file, and publishes no capability
that the connector's ledger/link protocol cannot prove.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.operation_completion import CompletionPinV1
from ..contracts.operation_payloads import (
    ConditionalWriteKind,
    ConnectorOperationProfileV1,
    ReconciliationPolicy,
)
from ..contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef
from ..orchestrator.operation_intent_sources import PreparedOperationIntentSources
from ..orchestrator.operation_materialization_inputs import (
    DeploymentOperationPolicyInputs,
    OperationMaterializationInputError,
)
from .connectors_publish import FilePublishConnector, PUBLISH_NAME
from .operation_payloads import ConnectorProfileRegistration, ConnectorProfileRegistry

_PARAMETERS_DOCUMENT = {
    "kind": "file-publish-parameters-v1",
    "required": ["artifact_path", "content_hash", "storage_uri"],
    "model_fields": ["artifact_path"],
}
_RETRY_POLICY_DOCUMENT = {
    "kind": "file-publish-retry-v1",
    "rule": "reconcile-before-retry",
    "maximum_new_send_attempts": 0,
}
_EVIDENCE_POLICY_DOCUMENT = {
    "kind": "file-publish-evidence-policy-v1",
    "required": ["ledger-intent", "published-file-readback", "content-hash"],
}
_MILESTONE_POLICY_DOCUMENT = {
    "kind": "file-publish-milestone-policy-v1",
    "milestones": {
        "FILE_PUBLISHED": "connector ledger intent plus nofollow published-file readback",
        "CONTENT_HASH_VERIFIED": "published-file readback sha256 equals frozen artifact hash",
    },
}
_RETENTION_DOCUMENT = {
    "kind": "file-publish-ledger-retention-v1",
    "basis": "connector-owned-ledger",
}


def _document_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _source_digest(connector: FilePublishConnector) -> tuple[Path, str]:
    source = inspect.getsourcefile(type(connector))
    if source is None:
        raise OperationMaterializationInputError(
            "OP_PROFILE_UNAVAILABLE", "loaded FilePublishConnector has no source artifact"
        )
    path = Path(source)
    try:
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise OperationMaterializationInputError(
            "OP_PROFILE_UNAVAILABLE", "FilePublishConnector source artifact is unreadable"
        ) from error


@dataclass(frozen=True, slots=True)
class _FilePublishProfileAdapter:
    """A profile facade over the registered concrete connector instance."""

    connector: FilePublishConnector
    profile: ConnectorOperationProfileV1

    def operation_profile(self) -> ConnectorOperationProfileV1:
        return self.profile


class BuiltinOperationProfiles(ConnectorProfileRegistry):
    """Registry and deployment-policy authority for built-in file publishing only."""

    def __init__(self, connectors: Mapping[str, Any]) -> None:
        candidate = connectors.get(PUBLISH_NAME)
        if type(candidate) is not FilePublishConnector:
            raise OperationMaterializationInputError(
                "OP_PROFILE_UNAVAILABLE", "trusted FilePublishConnector is not registered"
            )
        self._connector = candidate
        self._source_path, self._source_hash = _source_digest(candidate)
        self._implementation_ref = TypedRef(
            TypedRefKind.ARTIFACT,
            "file-publish-connector-source",
            1,
            self._source_hash,
        )
        self._profile = self._build_profile()
        self._adapter = _FilePublishProfileAdapter(candidate, self._profile)

    @property
    def retry_policy_ref(self) -> VersionedRef:
        return VersionedRef("file-publish-retry-v1", 1, _document_hash(_RETRY_POLICY_DOCUMENT))

    @property
    def evidence_policy_ref(self) -> CompletionPinV1:
        return CompletionPinV1(
            id="file-publish-evidence-policy-v1",
            revision=1,
            content_hash=_document_hash(_EVIDENCE_POLICY_DOCUMENT),
        )

    @property
    def milestone_policy_ref(self) -> CompletionPinV1:
        return CompletionPinV1(
            id="file-publish-milestone-policy-v1",
            revision=1,
            content_hash=_document_hash(_MILESTONE_POLICY_DOCUMENT),
        )

    @property
    def retention_basis_ref(self) -> TypedRef:
        return TypedRef(
            TypedRefKind.SOURCE,
            "file-publish-ledger-retention-v1",
            1,
            _document_hash(_RETENTION_DOCUMENT),
        )

    @property
    def supported_milestones(self) -> tuple[str, ...]:
        return ("FILE_PUBLISHED", "CONTENT_HASH_VERIFIED")

    @property
    def policy_documents(self) -> Mapping[str, Mapping[str, Any]]:
        """Canonical built-in documents to pin when approving an operation Spec."""
        return {
            "retry": dict(_RETRY_POLICY_DOCUMENT),
            "evidence": dict(_EVIDENCE_POLICY_DOCUMENT),
            "milestone": dict(_MILESTONE_POLICY_DOCUMENT),
            "retention": dict(_RETENTION_DOCUMENT),
        }

    def _idempotency_contract(self) -> Mapping[str, Any]:
        scope = hashlib.sha256(
            f"{self._connector.root}\0{self._connector.ledger_path}".encode("utf-8")
        ).hexdigest()
        return {
            "kind": "ATOMIC_KEY",
            "namespace": f"file-publish-{scope}",
            "retention_basis_ref": self.retention_basis_ref.to_json(),
            "minimum_retention_ms": 0,
            "conflict_protocol_id": "file-publish-ledger-v1",
        }

    def _build_profile(self) -> ConnectorOperationProfileV1:
        root_scope = hashlib.sha256(str(self._connector.root).encode("utf-8")).hexdigest()
        ledger_scope = hashlib.sha256(str(self._connector.ledger_path).encode("utf-8")).hexdigest()
        return ConnectorOperationProfileV1(
            1,
            PUBLISH_NAME,
            f"file-publish-{self._source_hash[:16]}",
            self._source_hash,
            {
                "service_id": PUBLISH_NAME,
                "account_scope": root_scope,
                "environment": f"ledger-{ledger_scope[:24]}",
            },
            "publish",
            VersionedRef("file-publish-parameters-v1", 1, _document_hash(_PARAMETERS_DOCUMENT)),
            "file-publish-outcome-v1",
            self.supported_milestones,
            {"id": "file-publish-ledger", "version": "1"},
            ConditionalWriteKind.NONE,
            self._idempotency_contract(),
            (),
            TypedRef(
                TypedRefKind.SOURCE,
                "file-publish-connector-source",
                1,
                self._source_hash,
            ),
        )

    def resolve_connector_operation(
        self, connector_id: str, operation_name: str
    ) -> ConnectorProfileRegistration | None:
        if connector_id != PUBLISH_NAME or operation_name != "publish":
            return None
        return ConnectorProfileRegistration(
            adapter=self._adapter,
            implementation_ref=self._implementation_ref,
            profile=self._profile,
        )

    def policy_for(
        self, sources: PreparedOperationIntentSources
    ) -> DeploymentOperationPolicyInputs:
        """Return only the built-in policy whose evidence pin was approved in the Spec."""
        if not isinstance(sources, PreparedOperationIntentSources):
            raise OperationMaterializationInputError("OP_INTENT_SOURCE_UNRESOLVED", "typed sources")
        if sources.effect.required_milestone not in self.supported_milestones:
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "approved Spec requires unsupported milestone"
            )
        if sources.effect.evidence_policy_ref != self.evidence_policy_ref:
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "approved evidence policy is not file-publish policy"
            )
        if sources.effect.milestone_policy_ref != self.milestone_policy_ref:
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "approved milestone policy is not file-publish policy"
            )
        return DeploymentOperationPolicyInputs(
            retry_policy_ref=self.retry_policy_ref,
            required_target_condition=ConditionalWriteKind.NONE,
            reconciliation_policy=ReconciliationPolicy.RECONCILE_ONLY,
            idempotency_contract=self._profile.idempotency,
            evidence_policy_ref=self.evidence_policy_ref,
        )


__all__ = ("BuiltinOperationProfiles",)


class DeploymentOperationProfiles:
    """Compose explicit runtime registries without inventing connector policy."""

    def __init__(self, *registries: Any) -> None:
        if not registries:
            raise ValueError("operation deployment requires actual registries")
        self.registries = tuple(registries)

    def policy_registry(self, connector_id: str, operation_name: str) -> Any:
        matches = [registry for registry in self.registries
                   if registry.resolve_connector_operation(connector_id, operation_name) is not None]
        if len(matches) != 1:
            raise OperationMaterializationInputError("OP_PROFILE_UNAVAILABLE", "ambiguous or absent registration")
        return matches[0]

    def resolve_connector_operation(self, connector_id: str, operation_name: str) -> ConnectorProfileRegistration:
        registration = self.policy_registry(connector_id, operation_name).resolve_connector_operation(
            connector_id, operation_name)
        if not isinstance(registration, ConnectorProfileRegistration):
            raise OperationMaterializationInputError("OP_PROFILE_UNAVAILABLE")
        return registration

    def policy_for(self, sources: PreparedOperationIntentSources) -> DeploymentOperationPolicyInputs:
        matches = [registry for registry in self.registries
                   if registry.milestone_policy_ref == sources.effect.milestone_policy_ref
                   and registry.evidence_policy_ref == sources.effect.evidence_policy_ref]
        if len(matches) != 1:
            raise OperationMaterializationInputError("OP_CAPABILITY_UNSUPPORTED", "no unique approved policy")
        policy = matches[0].policy_for(sources)
        if not isinstance(policy, DeploymentOperationPolicyInputs):
            raise OperationMaterializationInputError("OP_CAPABILITY_UNSUPPORTED", "untyped deployment policy")
        return policy
