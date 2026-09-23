# SPDX-License-Identifier: Apache-2.0
"""Exact-contract outcomes for TaskGraph ORDER and composition reads."""
from __future__ import annotations

import json

from ..contracts.evidence_state import WitnessDecision, WitnessPurpose
from ..contracts.htn import OccurrenceId, TaskForm
from ..contracts.models import sha256_hex
from ..contracts.semantic_base import TypedRefKind
from ..graph.eligibility import OccurrenceOutcome
from ..graph.task_network import TaskNetworkSnapshot
from ..runtime.planning_operations import SourceUnavailable
from ..storage.htn_store import HtnStore
from ..storage.store import Store


def read_taskgraph_outcomes(store: Store, mission_id: str,
                           network: TaskNetworkSnapshot) -> dict[OccurrenceId, OccurrenceOutcome]:
    with store.read_view() as db:
        semantics = HtnStore(store)
        requirements = semantics.latest_requirements_revision(mission_id)
        if requirements is None:
            raise SourceUnavailable("taskgraph_outcome_requirements_missing")
        from .scoped_content_review import uses_completion_protocol
        if uses_completion_protocol(store, mission_id):
            return _completion_outcomes(store, mission_id, network)
        receipts = {}
        for receipt in semantics.list_acceptance_receipts(mission_id):
            identity = (receipt.kind, receipt.subject_id)
            if identity in receipts:
                raise SourceUnavailable("taskgraph_outcome_commit_receipt_ambiguous")
            receipts[identity] = receipt

        def usable(kind: str, identity: str, body: dict, task_id: str) -> bool:
            receipt = receipts.get((kind, identity))
            if receipt is None:
                raise SourceUnavailable("taskgraph_outcome_commit_receipt_missing")
            event = db.execute("SELECT mission_id,type,payload_json FROM events WHERE event_id=?", (receipt.event_id,)).fetchone()
            event_type = "AcceptanceCommitted" if kind == "acceptance" else "GoalResolutionCommitted"
            if event is None or event["mission_id"] != mission_id or event["type"] != event_type:
                raise SourceUnavailable("taskgraph_outcome_commit_event_missing")
            payload = json.loads(event["payload_json"])
            output = dict(receipt.output_identity)
            if (output.get("kind") != kind or output.get("subject_id") != identity
                    or output.get("content_hash") != sha256_hex(body)
                    or payload.get("output_identity") != output or payload.get("command_id") != receipt.command_id
                    or payload.get("intent_hash") != receipt.intent_hash or payload.get("read_set_hash") != receipt.read_set_hash):
                raise SourceUnavailable("taskgraph_outcome_commit_identity_mismatch")
            witness_id = payload.get("witness_id")
            if not isinstance(witness_id, str) or not witness_id:
                raise SourceUnavailable("taskgraph_outcome_witness_missing")
            witness = semantics.get_validity_witness(witness_id)
            if (witness.purpose is not WitnessPurpose.ACCEPT or witness.consumer_ref.kind is not TypedRefKind.TASK
                    or str(witness.consumer_ref.id) != task_id):
                raise SourceUnavailable("taskgraph_outcome_witness_identity_mismatch")
            # Original Commit checked freshness/epoch when accepting. ORDER
            # records that accepted predecessor; expiry does not erase that
            # historical fact. Live consumer/guard and DATA licences are checked
            # separately by readiness and the input resolver.
            return witness.decision is WitnessDecision.USABLE

        accepted = set()
        for item in semantics.list_acceptances(mission_id):
            if str(item.validity) != "CURRENT" or item.requirements_revision != int(requirements.revision):
                continue
            task_id = str(item.task_id)
            if usable("acceptance", str(item.acceptance_id), item.to_json(), task_id):
                accepted.add((task_id, str(item.obligation_id), item.contract_revision))
        resolved = set()
        for resolution in semantics.list_goal_resolutions(mission_id):
            if (str(resolution.validity) != "CURRENT" or str(resolution.verdict) != "ACCEPT"
                    or resolution.requirements_version != int(requirements.revision)):
                continue
            if usable("goal_resolution", str(resolution.resolution_id), resolution.to_json(), resolution.goal_task_id):
                resolved.add((resolution.goal_task_id, str(resolution.obligation_id), resolution.contract_revision))
        outcomes = {}
        for spec in network.occurrences:
            binding = network.binding_for_occurrence(spec.occurrence_id)
            key = (str(spec.task_id), str(spec.obligation_id), int(binding.contract_revision))
            if key in (resolved if spec.form is TaskForm.COMPOUND else accepted):
                outcomes[spec.occurrence_id] = OccurrenceOutcome.ACCEPTED
                continue
            task = store.get_task(str(spec.task_id))
            if task is None or task.mission_id != mission_id:
                raise SourceUnavailable("taskgraph_outcome_task_missing")
            from .taskgraph_terminal import read_terminal_outcome
            outcomes[spec.occurrence_id] = read_terminal_outcome(store, task, binding)
        return outcomes


def _completion_outcomes(store: Store, mission_id: str,
                         network: TaskNetworkSnapshot) -> dict[OccurrenceId, OccurrenceOutcome]:
    from .completion_status import read_occurrence_completion
    from .operation_completion import OperationCompletionError
    from .taskgraph_terminal import read_terminal_outcome
    semantics = HtnStore(store)
    active = semantics.active_plan_revision(mission_id)
    outcomes = {}
    for occurrence in network.occurrences:
        binding = network.binding_for_occurrence(occurrence.occurrence_id)
        status = None
        if active is not None:
            try:
                status = read_occurrence_completion(store, mission_id, str(occurrence.occurrence_id))
            except OperationCompletionError as error:
                if error.code not in {"OP_COMPLETION_SCOPE_UNRESOLVED", "OP_REQUIREMENT_MAPPING_MISSING"}:
                    raise
        if status is not None and status.complete:
            # Exact original content review + EffectFulfillment + current scope.
            # An Action success or a content-only Acceptance is insufficient.
            outcomes[occurrence.occurrence_id] = OccurrenceOutcome.ACCEPTED
            continue
        task = store.get_task(str(occurrence.task_id))
        if task is None:
            # Compound roots are original semantic objects before a legacy Task
            # row is materialized. This means unfinished, never accepted/settled.
            original = semantics.task_semantics_of(mission_id, str(occurrence.task_id))
            if occurrence.form is not TaskForm.COMPOUND or original is None:
                raise SourceUnavailable("taskgraph_outcome_task_missing")
            outcomes[occurrence.occurrence_id] = OccurrenceOutcome.RUNNING
        elif task.mission_id != mission_id:
            raise SourceUnavailable("taskgraph_outcome_task_owner_mismatch")
        else:
            outcomes[occurrence.occurrence_id] = read_terminal_outcome(store, task, binding)
    return outcomes
