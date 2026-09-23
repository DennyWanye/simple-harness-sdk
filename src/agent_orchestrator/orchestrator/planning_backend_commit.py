# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Map a solver's complete decomposition back to a frozen SH Plan command.

Solving occurs outside the writer. The final write uses the same H1-H admission
and CommitService as model-produced plans; a solver cannot mint write authority.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..contracts.htn import OccurrenceId, TaskForm
from ..contracts.models import ContractError
from ..planning.htn.backend_port import CandidatePlanWitness, PlanningProblemSnapshot
from ..planning.htn.validation import HddlExport, _export_roots, _hddl_name, to_hddl
from ..storage.htn_store import PlanCommitReceipt
from .plan_commits import CommitPlanCommand, PlanPrincipal
from .planning_admission_commits import PlanningCommitAdmission


def _verify_mapping(
    command: CommitPlanCommand, exported: HddlExport, witness: CandidatePlanWitness
) -> None:
    """Check identities, complete method membership, and every declared ordering."""
    if not witness.valid_hash() or not witness.hierarchy_json:
        raise ContractError("solver witness lacks its complete decomposition")
    try:
        body = json.loads(witness.hierarchy_json)
        primitives = body["primitives"]
        decompositions = body["decompositions"]
        roots = tuple(body["roots"])
        if not isinstance(primitives, list) or not isinstance(decompositions, list):
            raise ValueError("witness nodes must be lists")
        by_id = {item["plan_id"]: item for item in (*primitives, *decompositions)}
        if len(by_id) != len(primitives) + len(decompositions) or len(set(roots)) != len(roots):
            raise ValueError("duplicate witness identities")
        labels = {label: occurrence for occurrence, label in exported.occurrence_map.items()}
        if len(labels) != len(exported.occurrence_map):
            raise ValueError("HDDL occurrence labels collide")
        nodes: dict[str, Any] = {}
        parents: dict[str, str] = {}
        for item in decompositions:
            if item["task_name"] != "t_available" or len(item["task_arguments"]) != 1:
                raise ValueError("unknown abstract task")
            occurrence = labels[item["task_arguments"][0]]
            if occurrence in nodes:
                raise ValueError("occurrence decomposed more than once")
            nodes[occurrence] = item
            for child in item["children"]:
                if child not in by_id or child in parents:
                    raise ValueError("child missing or shared by two witness parents")
                parents[child] = item["plan_id"]
        expected_roots = {str(item) for item in _export_roots(command.delta)}
        actual_roots = {labels[by_id[root]["task_arguments"][0]] for root in roots}
        if (
            actual_roots != expected_roots
            or set(nodes) != set(exported.occurrence_map)
            or set(by_id) - set(parents) != set(roots)
        ):
            raise ValueError("witness roots or occurrence coverage differ")
        if (
            tuple(" ".join((item["name"], *item["arguments"])) for item in primitives)
            != witness.steps
        ):
            raise ValueError("primitive sequence differs from witness")
        if [item["position"] for item in primitives] != list(range(len(primitives))):
            raise ValueError("primitive positions are not consecutive")
        primitive_ids = {item["plan_id"] for item in primitives}
        draft_by_occ = {
            str(item.effective_goal_occurrence_id): item for item in command.delta.method_instances
        }
        for occurrence, node in nodes.items():
            label = exported.occurrence_map[occurrence]
            spec = command.network.occurrence(OccurrenceId(occurrence))
            children = tuple(node["children"])
            if occurrence in exported.reused_occurrences:
                if node["method_name"] != f"m_reuse_{label}" or children:
                    raise ValueError("reused result was not mapped to the reuse method")
            elif spec.form is TaskForm.PRIMITIVE:
                if (
                    node["method_name"] != f"m_do_{label}"
                    or len(children) != 1
                    or children[0] not in primitive_ids
                ):
                    raise ValueError("primitive occurrence has a different method")
                child = by_id[children[0]]
                signature = command.network.binding_for_task(
                    spec.task_id
                ).goal_signature.signature_id
                if child["name"] != "a_" + _hddl_name(signature) or child["arguments"] != [label]:
                    raise ValueError("primitive action does not map to its SH Task")
            else:
                instance = draft_by_occ.get(occurrence) or command.network.adopted_instance_for(
                    spec.occurrence_id
                )
                if instance is None or node["method_name"] != "m_" + _hddl_name(
                    str(instance.instance_id)
                ):
                    raise ValueError("compound method differs from compiler output")
                expected = Counter(str(child.occurrence_id) for child in instance.child_bindings)
                actual = Counter(labels[by_id[child]["task_arguments"][0]] for child in children)
                if actual != expected:
                    raise ValueError("method child membership differs from compiler output")
        visited: set[str] = set()
        active: set[str] = set()
        spans: dict[str, tuple[int, ...]] = {}

        def visit(node_id: str) -> tuple[int, ...]:
            if node_id in active or node_id in visited:
                raise ValueError("cycle or duplicate witness traversal")
            active.add(node_id)
            node = by_id[node_id]
            positions = (
                (node["position"],)
                if node_id in primitive_ids
                else tuple(pos for child in node["children"] for pos in visit(child))
            )
            active.remove(node_id)
            visited.add(node_id)
            spans[node_id] = positions
            return positions

        for root in roots:
            visit(root)
        if visited != set(by_id):
            raise ValueError("disconnected witness nodes")
        edges = [(str(item.before), str(item.after)) for item in command.network.order_constraints]
        edges.extend(
            (str(item.producer_occurrence), str(item.consumer_occurrence))
            for item in command.network.data_requirements
        )
        for before, after in edges:
            if before not in nodes or after not in nodes:
                continue
            left, right = spans[nodes[before]["plan_id"]], spans[nodes[after]["plan_id"]]
            if left and right and max(left) >= min(right):
                raise ValueError("solver sequence violates an ORDER/DATA edge")
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ContractError(f"solver witness cannot map back to SH: {error}") from error


@dataclass(frozen=True, slots=True)
class SolverPlanCommitLane:
    commit_service: Any
    command: CommitPlanCommand
    principal: PlanPrincipal
    admission: PlanningCommitAdmission

    def snapshot(self) -> PlanningProblemSnapshot:
        exported = to_hddl(self.command.delta, network=self.command.network)
        if not isinstance(exported, HddlExport):
            raise ContractError("UNSUPPORTED_FEATURE: " + exported.detail)
        return PlanningProblemSnapshot.build(
            self.command.mission_id,
            int(self.command.delta.base_plan_revision),
            {
                "domain_text": exported.domain_text,
                "problem_text": exported.problem_text,
                "occurrence_map": dict(exported.occurrence_map),
                "command_hash": self.command.intent_hash(),
                "admission_request_id": self.admission.request_id,
                "admission_decision_hash": self.admission.decision_hash,
            },
        )

    def commit_backend_plan(
        self, snapshot: PlanningProblemSnapshot, witness: CandidatePlanWitness
    ) -> PlanCommitReceipt:
        if self.snapshot().digest != snapshot.digest or witness.snapshot_digest != snapshot.digest:
            raise ContractError("solver snapshot differs from the frozen SH command")
        exported = to_hddl(self.command.delta, network=self.command.network)
        if not isinstance(exported, HddlExport):
            raise ContractError("UNSUPPORTED_FEATURE: " + exported.detail)
        _verify_mapping(self.command, exported, witness)
        receipt = self.commit_service.commit_planning_revision(
            self.command, self.principal, admission=self.admission
        )
        if not isinstance(receipt, PlanCommitReceipt):
            raise ContractError("CommitService returned no durable PlanCommitReceipt")
        return receipt
