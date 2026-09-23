# SPDX-License-Identifier: Apache-2.0
"""Produce the fixed twelve-channel plan snapshot from actual original readers.

Execution policy is read by the fixed original-policy adapter. Reliably imported
external runtime facts remain mandatory; no local empty set substitutes for them.
The provider must verify its original receipts/currentness; it may not do external
IO inside this Store view. It supplies facts, never a candidate or a PASS verdict.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Protocol

from simple_harness.contracts import canonical_json

from ..contracts.models import sha256_hex
from ..contracts.planning_decisions import PlanningRefV1
from ..contracts.semantic_base import TypedRef, TypedRefKind
from ..governance.planning_authorization import (
    PlanningAuthorizationSnapshot, planning_policy_for_mission, StorePlanningAuthorityReader,
    build_planning_authorization,
)
from ..graph.execution_contracts import CompleteRead, GraphSourceRead
from ..graph.taskgraph_sharing import SharingSources
from ..graph.eligibility import OccurrenceOutcome
from .hierarchical_dispatch import shared_goal_index
from .taskgraph_demands import independently_required_occurrences
from ..planning.plan_preview import _source_snapshot_payload
from ..runtime.planning_operations import SourceUnavailable, StoreOperationReader, build_operation_snapshot
from ..runtime.taskgraph_operation_sources import read_operation_producers
from ..storage.htn_store import HtnStore
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.store import Store
from .plan_commits import PlanPrincipal
from .taskgraph_execution_sources import LocalExecutionSources, TaskGraphLocalExecutionReader, _document
from .taskgraph_preview import PlanMutationSources
from .taskgraph_sharing_inputs import read_shared_inputs


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportedExecutionSource:
    mission_id: str
    kind: str
    canonical_document: str

    def __post_init__(self) -> None:
        value = json.loads(self.canonical_document)
        if (self.kind not in {"runtime", "execution_policy"} or not isinstance(value, dict)
                or not self.mission_id or canonical_json(value) != self.canonical_document
                or value.get("mission_id") != self.mission_id):
            raise ValueError("imported execution source must be a canonical Mission-bound document")

    def to_json(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "kind": self.kind, "document": json.loads(self.canonical_document)}


class ExecutionImports(Protocol):
    def read_runtime(self, store: Store, mission_id: str) -> CompleteRead[ImportedExecutionSource]:
        """Complete physical/provider/tool inventory through the original reliable importer."""
        ...

    def read_execution_policy(self, store: Store, mission_id: str) -> CompleteRead[ImportedExecutionSource]:
        """Current execution grants, permission intersection and resource policy receipts."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionReadContext:
    local: LocalExecutionSources
    running_work: CompleteRead[ImportedExecutionSource]
    execution_policy: CompleteRead[ImportedExecutionSource]
    operation_snapshot: CompleteRead[Any]


class TaskGraphPlanSourceReader:
    def __init__(self, local: TaskGraphLocalExecutionReader, *, imports: ExecutionImports) -> None:
        if not callable(getattr(imports, "read_runtime", None)) or not callable(getattr(imports, "read_execution_policy", None)):
            raise ValueError("actual H1 runtime and execution-policy producers are mandatory")
        self.local, self.store, self.imports = local, local.store, imports

    def read_execution(self, mission_id: str, *, materializing: bool = False) -> ExecutionReadContext:
        with self.store.read_view():
            local = self.local.read(mission_id, materializing=materializing)
            through = local.structure.token.through_seq
            def imported(kind: str, value: CompleteRead[ImportedExecutionSource]) -> CompleteRead[ImportedExecutionSource]:
                if (not isinstance(value, CompleteRead) or not isinstance(value.value, ImportedExecutionSource)
                        or value.value.mission_id != mission_id or value.value.kind != kind
                        or value.through_seq > through or value.source_digest != sha256_hex(value.value.to_json())):
                    raise SourceUnavailable("taskgraph_execution_import_invalid", detail=kind)
                return value
            runtime = imported("runtime", self.imports.read_runtime(self.store, mission_id))
            policy = imported("execution_policy", self.imports.read_execution_policy(self.store, mission_id))
            operations = build_operation_snapshot(mission_id, reader=StoreOperationReader(self.store),
                                                  now_ms=int(self.store.now * 1000))
            return ExecutionReadContext(local=local, running_work=runtime, execution_policy=policy,
                operation_snapshot=CompleteRead(value=operations, source_id=mission_id+":operations",
                    source_digest=operations.read_digest, through_seq=through))

    def __call__(self, store: Store, request_id: str, decision_id: str,
                 principal: PlanPrincipal) -> PlanMutationSources:
        if store is not self.store:
            raise SourceUnavailable("taskgraph_plan_store_mismatch")
        with store.read_view() as db:
            decisions = PlanningDecisionStore(store)
            request = decisions.get_planning_request(request_id)
            decision = decisions.get_planning_decision(decision_id)
            if request is None or decision is None or decision["request_id"] != request_id:
                raise SourceUnavailable("taskgraph_plan_request_missing")
            raw = decision["canonical_json"]
            if not isinstance(raw, str) or not raw:
                raise SourceUnavailable("taskgraph_plan_decision_unreadable")
            import hashlib
            decision_body = json.loads(raw)
            if canonical_json(decision_body) != raw or hashlib.sha256(raw.encode()).hexdigest() != decision["canonical_hash"]:
                raise SourceUnavailable("taskgraph_plan_decision_hash_invalid")
            intent = store.get_intent(request.intent_id)
            if intent is None or intent.mission_id != request.mission_id:
                raise SourceUnavailable("taskgraph_plan_intent_missing")
            package = intent.config.get("planning_package")
            from collections.abc import Mapping
            if not isinstance(package, Mapping):
                raise SourceUnavailable("taskgraph_plan_package_missing")
            from ..planning.htn.planner_package import package_hash
            if package_hash(package) != request.package_hash:
                raise SourceUnavailable("taskgraph_plan_package_changed")
            visible = tuple(PlanningRefV1.from_json(item) for item in package.get("visible_refs", ()))
            execution = self.read_execution(request.mission_id)
            local = execution.local
            if local.structure.token.plan_revision != request.base_plan_revision:
                raise SourceUnavailable("taskgraph_plan_base_stale")
            authority = build_planning_authorization(request_id,
                read=StorePlanningAuthorityReader(PlanningAdmissionStore(store), store),
                caller=principal, policy=planning_policy_for_mission(store, request.mission_id), now_ms=int(store.now*1000))
            if not isinstance(authority, PlanningAuthorizationSnapshot):
                raise SourceUnavailable("taskgraph_plan_authority_unavailable")
            authority_body = authority.to_json()
            authority_body.pop("checked_at_ms", None)
            semantics = HtnStore(store)
            requirement = semantics.latest_requirements_revision(request.mission_id)
            if requirement is None or int(requirement.revision) != request.requirements_revision:
                raise SourceUnavailable("taskgraph_plan_requirements_stale")
            requirements_ref = TypedRef(kind=TypedRefKind.REQUIREMENTS, id=str(requirement.revision_id),
                revision=int(requirement.revision), content_hash=sha256_hex(requirement.to_json()))
            tasks = store.list_tasks(request.mission_id)
            from .taskgraph_bindings import current_bindings
            bindings = current_bindings(semantics, request.mission_id)
            world = self.local.dispatch_for(request.mission_id).require_planning_world()
            world_semantics = getattr(world, "semantics", None)
            if (not isinstance(world_semantics, HtnStore) or world_semantics._store is not store
                    or str(getattr(world, "mission_id", "")) != request.mission_id):
                raise SourceUnavailable("taskgraph_plan_evidence_store_unbound")
            methods = []
            method_definitions = []
            for ref in world.registry.method_refs():
                definition, registration = world.registry.definition(ref), world.registry.registration(ref)
                if definition is None or registration is None:
                    raise SourceUnavailable("taskgraph_plan_method_source_incomplete")
                method_definitions.append(definition)
                methods.append({"ref": ref.to_json(), "definition": definition.to_json(),
                    "registration": registration.to_json(), "receipt": _document(world.registry.receipt(ref)),
                    "trial_uses": world.registry.trial_uses(ref, mission_id=local.view.network.mission_id)})
            def receipt(value: CompleteRead[Any]) -> dict[str, Any]:
                # Event sequence is the snapshot boundary, not a source identity.
                # An unrelated event cannot invalidate otherwise identical inputs.
                return {"source_id": value.source_id, "digest": value.source_digest, "value": _document(value.value)}
            acceptances = semantics.list_acceptances(request.mission_id)
            sharing_entries = []
            sharing_inputs = {}
            for entry in shared_goal_index(local.view.network, catalog=world.catalog).entries():
                binding = local.view.network.binding_for_occurrence(entry.occurrence_id)
                outcome = local.view.outcomes[entry.occurrence_id]
                accepted = None
                if outcome is OccurrenceOutcome.ACCEPTED:
                    eligible = [item for item in acceptances
                        if str(item.task_id) == str(entry.task_id)
                        and item.obligation_id == entry.obligation_id
                        and item.contract_revision == int(binding.contract_revision)
                        and item.requirements_revision == int(requirement.revision)
                        and str(item.validity) == "CURRENT"]
                    # Multiple original Acceptances require an explicit choice;
                    # neither insertion order nor the newest timestamp grants one.
                    if len(eligible) != 1:
                        continue
                    accepted = eligible[0]
                    entry = replace(entry, acceptance_ref=TypedRef(kind=TypedRefKind.ACCEPTANCE,
                        id=str(accepted.acceptance_id), revision=accepted.contract_revision,
                        content_hash=sha256_hex(accepted.to_json())))
                elif outcome is not OccurrenceOutcome.RUNNING:
                    continue
                proof = read_shared_inputs(self.local, local, entry, accepted)
                if proof is None:
                    continue
                sharing_entries.append(entry)
                sharing_inputs[str(entry.occurrence_id)] = proof
            operation_producers = read_operation_producers(store, execution.operation_snapshot.value)
            from .taskgraph_completion_sources import read_completion_sources
            completion = read_completion_sources(store, request.mission_id, local.view.network)
            network_body = _source_snapshot_payload(local.view.network)
            evidence = world.snapshot()
            predicates = tuple(world.predicates.signatures())
            # Settlement advances monetary/token/attempt counters without changing
            # who still wants the work. Keep the complete account in the budget
            # channel, which is rechecked on continuation, and retain all demand,
            # lifecycle, failure/fuel and shape facts in the immutable demand lane.
            obligation_accounts = [item.to_json() for item in local.obligation_accounts.value]
            demand_accounts = [{key: value for key, value in account.items()
                if key not in {"consumed_cost_micros", "consumed_tokens", "consumed_attempts"}}
                for account in obligation_accounts]
            bodies = {
                "request": {"request": request.to_json(), "decision": decision_body,
                    "visible_refs": [ref.to_json() for ref in visible],
                    "planning_subjects": _document(package.get("planning_subjects", ()))},
                "authorization": {"planning": authority_body, "execution": receipt(execution.execution_policy)},
                "network": {"document": local.structure.document.to_json(), "current": network_body,
                    "bindings": [item.to_json() for item in bindings], "requirements": requirement.to_json()},
                "methods": {"store": _document(local.current_method_registrations.value), "registry": methods,
                    "catalog": [item.to_json() for item in world.catalog.task_types()],
                    "schemas": [item.to_json() for item in world.schemas.schemas()]},
                "evidence": {"world": _document(evidence), "witnesses": _document(local.witnesses.value),
                    "epochs": _document(local.scope_epochs.value),
                    "predicates": sorted((item.to_json() for item in predicates), key=canonical_json)},
                "capabilities": _document(local.deployment_capabilities.value),
                "operations": {"snapshot": receipt(execution.operation_snapshot),
                    "producers": _document(operation_producers)},
                "running_work": {"local": local.local_work.value.to_json(), "import": receipt(execution.running_work)},
                "acceptances": {"outputs": _document(local.accepted_outputs.value),
                    "acceptances": _document(acceptances),
                    "completion": _document(completion),
                    "resolutions": _document(semantics.list_goal_resolutions(request.mission_id)),
                    "receipts": _document(semantics.list_acceptance_receipts(request.mission_id))},
                "demand": {"accounts": demand_accounts,
                    "obligations": _document(local.obligations.value), "slots": _document(local.active_demands.value),
                    "independent": _document(local.independent_obligations.value)},
                "budget": {"accounts": _document(local.budget_snapshots.value),
                    "obligation_accounts": obligation_accounts,
                    "resource_policy": receipt(execution.execution_policy)},
                "inputs": {"policy": local.source_input_policy.source_digest,
                    "targets": _document(local.target_rules.value), "manifests": [dict(row) for row in db.execute(
                        "SELECT DISTINCT m.* FROM input_manifests m JOIN input_manifest_bindings b "
                        "ON b.manifest_hash=m.manifest_hash WHERE b.mission_id=? ORDER BY m.manifest_hash",
                        (request.mission_id,))], "manifest_bindings": [dict(row) for row in db.execute(
                        "SELECT * FROM input_manifest_bindings WHERE mission_id=? ORDER BY task_id,manifest_hash",
                        (request.mission_id,))]},
            }
            settlement_reader = self.local.dispatch_for(request.mission_id)._taskgraph_settlement_reader
            if not callable(settlement_reader):
                raise SourceUnavailable("taskgraph_sharing_settlement_source_missing")
            settlements = settlement_reader(request.mission_id, local.view.network, local.view.outcomes,
                predecessors=frozenset(item.occurrence_id for item in local.view.network.occurrences))
            sharing = SharingSources(entries=tuple(sharing_entries), input_proofs=sharing_inputs,
                outcomes=local.view.outcomes, settlements=settlements,
                starts=local.view.starts, epochs=dict(local.scope_epochs.value), evidence=evidence,
                predicates=predicates, methods=tuple(method_definitions),
                now_ms=int(store.now * 1000), independent_required=independently_required_occurrences(
                    local.structure.document, local.independent_obligations.value))
            bodies["acceptances"]["sharing_entries"] = _document(sharing.entries)
            # The eligible sharing set can change when an original Attempt
            # settles. Actual immutable manifest bytes stay in the inputs lane;
            # fresh eligibility is revalidated like Acceptance, not frozen as a
            # new semantic input merely because its source Attempt finished.
            bodies["acceptances"]["sharing_input_proofs"] = _document(sharing.input_proofs)
            bodies["acceptances"]["outcomes"] = _document(sharing.outcomes)
            bodies["running_work"]["settlements"] = _document(sharing.settlements)
            bodies["demand"]["independent_required_occurrences"] = sorted(sharing.independent_required)
            reads = tuple(GraphSourceRead(channel=channel, identity=f"{request.mission_id}:{channel}",
                digest=sha256_hex(body), coverage="COMPLETE") for channel, body in sorted(bodies.items()))
            return PlanMutationSources(mission_id=request.mission_id, request_id=request_id,
                decision_id=decision_id, decision_hash=decision["canonical_hash"],
                source_snapshot_hash=sha256_hex(network_body), before=local.structure.document,
                requirements_ref=requirements_ref, current_bindings=bindings,
                existing_task_ids=frozenset(task.id for task in tasks), source_reads=reads, sharing=sharing, operation_producers=operation_producers)
