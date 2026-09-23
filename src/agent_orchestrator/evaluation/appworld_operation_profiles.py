# SPDX-License-Identifier: Apache-2.0
"""Registered AppWorld command provenance plus independent database-state readback."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..contracts.operation_completion import CompletionPinV1
from ..contracts.operation_payloads import ConditionalWriteKind, ConnectorOperationProfileV1, OperationParametersV1, ReconciliationPolicy
from ..contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef, content_hash_of
from ..orchestrator.operation_materialization_inputs import DeploymentOperationPolicyInputs, OperationMaterializationInputError
from ..runtime.connectors import Receipt
from ..runtime.operation_outcomes import ObservedOperationMilestone, OutcomeReceiptError
from ..runtime.operation_payloads import ConnectorProfileRegistration
from .appworld_operations import AppWorldOperationConnector
from .appworld_state_observations import AppWorldStatePolicy

MILESTONE = "TASK_STATE_SATISFIED_AT_WORLD_VERSION"


def state_policy_refs(policy: AppWorldStatePolicy) -> tuple[CompletionPinV1, CompletionPinV1]:
    """Pure pins for an explicitly selected policy, usable before opening an episode."""
    evidence = CompletionPinV1(id="appworld-state-evidence-v1", revision=1,
        content_hash=content_hash_of({"state_policy_hash": policy.content_hash,
                                    "checks": list(AppWorldOperationProfiles.executed_check_ids)}))
    milestone = CompletionPinV1(id="appworld-state-milestone-v1", revision=1,
        content_hash=content_hash_of({"milestone": MILESTONE,
            "meaning": "registered task predicates held at the recorded world identity",
            "state_policy_hash": policy.content_hash, "official_evaluator_used": False}))
    return evidence, milestone


class AppWorldOperationProfiles:
    """One explicit episode and one frozen task-state policy, with no scoring API."""
    adapter_id = "appworld-journal-state"
    adapter_version = "1"
    executed_check_ids = ("appworld-command-journal", "appworld-registered-state", "appworld-world-identity")

    def __init__(self, connector: AppWorldOperationConnector, policy: AppWorldStatePolicy):
        if type(connector) is not AppWorldOperationConnector or policy.to_json()["task_id"] != connector.episode.config.task_id:
            raise ValueError("AppWorld profile requires its actual episode and state policy")
        self.connector, self.policy = connector, policy
        # Include the connector, state reader and profile interpreter, not just a
        # claimed package version, in the frozen executable identity.
        self.source_hash = content_hash_of({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("appworld_operations.py", "appworld_state_observations.py", "appworld_operation_profiles.py",
                         "appworld.py", "appworld_service.py", "appworld_resume.py")})
        self.namespace = {"service_id": connector.name, "account_scope": content_hash_of(connector.identity),
                          "environment": "appworld-isolated-benchmark"}
        self.retention_ref = TypedRef(TypedRefKind.SOURCE, "appworld-command-journal-retention", 1,
            content_hash_of({"directory": str(connector.root), "retain": "entire-episode-and-reconciliation",
                             "missing_journal": "UNKNOWN"}))
        self.idempotency = {"kind": "ATOMIC_KEY", "namespace": content_hash_of(self.namespace),
            "retention_basis_ref": self.retention_ref.to_json(), "minimum_retention_ms": 0,
            "conflict_protocol_id": "appworld-command-journal-v1"}
        self.profile = ConnectorOperationProfileV1(1, connector.name,
            "appworld-journal-" + self.source_hash[:16], self.source_hash, self.namespace, "execute",
            VersionedRef("appworld-approved-program-v1", 1, content_hash_of({"model_fields": ["artifact_path"],
                "required": ["artifact_path", "content_hash", "storage_uri", "size"]})),
            "appworld-state-outcome-v1", (MILESTONE,), {"id": self.adapter_id, "version": self.adapter_version},
            ConditionalWriteKind.NONE, self.idempotency, (),
            TypedRef(TypedRefKind.SOURCE, "appworld-operation-source", 1, self.source_hash))

    @property
    def evidence_policy_ref(self) -> CompletionPinV1:
        return state_policy_refs(self.policy)[0]

    @property
    def milestone_policy_ref(self) -> CompletionPinV1:
        return state_policy_refs(self.policy)[1]

    def operation_profile(self) -> ConnectorOperationProfileV1:
        return self.profile

    def resolve_connector_operation(self, connector_id: str, operation_name: str) -> ConnectorProfileRegistration | None:
        if (connector_id, operation_name) != (self.connector.name, "execute"):
            return None
        return ConnectorProfileRegistration(self, TypedRef(TypedRefKind.ARTIFACT,
            "appworld-operation-source", 1, self.source_hash), self.profile, self)

    def policy_for(self, sources: Any) -> DeploymentOperationPolicyInputs:
        if (sources.effect.required_milestone != MILESTONE
                or sources.effect.evidence_policy_ref != self.evidence_policy_ref
                or sources.effect.milestone_policy_ref != self.milestone_policy_ref):
            raise OperationMaterializationInputError("OP_CAPABILITY_UNSUPPORTED", "AppWorld state policy differs")
        return DeploymentOperationPolicyInputs(
            retry_policy_ref=VersionedRef("appworld-no-resend-v1", 1,
                content_hash_of({"rule": "RECONCILE_ONLY", "maximum_new_send_attempts": 0})),
            required_target_condition=ConditionalWriteKind.NONE,
            reconciliation_policy=ReconciliationPolicy.RECONCILE_ONLY,
            idempotency_contract=self.idempotency, evidence_policy_ref=self.evidence_policy_ref)

    def interpret(self, receipt: Receipt, *, action: Mapping[str, Any], parameters: OperationParametersV1,
                  handoff_events: Sequence[Any], required_milestone: str) -> ObservedOperationMilestone:
        def refuse(detail: str) -> None:
            raise OutcomeReceiptError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", detail)
        if required_milestone != MILESTONE or not receipt.applied:
            refuse("unsupported milestone or unapplied execution")
        if (receipt.connector, receipt.operation, receipt.target, receipt.params_hash, receipt.idempotency_key) != (
                self.connector.name, "execute", self.connector.episode.config.task_id,
                parameters.params_hash, action.get("idempotency_key")):
            refuse("execution identity differs")
        if (parameters.connector_id, parameters.operation_name, parameters.normalized_target_ref) != (
                receipt.connector, receipt.operation, receipt.target):
            refuse("frozen parameters differ")
        found = self.connector.lookup(receipt.idempotency_key)
        if found is None or found.to_json() != receipt.to_json():
            refuse("original command journal differs")
        ids, ordinals = [], []
        for event in handoff_events:
            if (event.type != "ActionHandedOff" or event.mission_id != action.get("mission_id")
                    or event.payload.get("action_key") != action.get("action_key")
                    or event.payload.get("idempotency_key") != receipt.idempotency_key
                    or type(event.payload.get("handoff")) is not int):
                refuse("original handoff differs")
            ids.append(str(event.id))
            ordinals.append(event.payload["handoff"])
        if (not ids or len(ids) != action.get("handoffs") or len(set(ids)) != len(ids)
                or sorted(ordinals) != list(range(1, len(ids) + 1))):
            refuse("original handoff coverage differs")
        execution = receipt.after.get("execution") if isinstance(receipt.after, Mapping) else None
        if not isinstance(execution, Mapping):
            refuse("execution provenance is unavailable")
        assert isinstance(execution, Mapping)
        original = self.connector.episode.get_execution_receipt(str(execution.get("execution_id", "")))
        if original is None or asdict(original) != dict(execution):
            refuse("execution is outside the original episode")
        assert original is not None
        observation, result = self.connector.episode.observe_registered_state(self.policy)
        if (not result["satisfied"] or observation.world_version < original.world_version
                or not self.connector.episode.verify_state_observation(observation, result)):
            refuse("registered task predicates do not hold in the current world")
        return ObservedOperationMilestone(MILESTONE, {"receipt": receipt.to_json(),
            "state_observation": asdict(observation), "state_result": result,
            "executed_check_ids": list(self.executed_check_ids)}, tuple(ids))
