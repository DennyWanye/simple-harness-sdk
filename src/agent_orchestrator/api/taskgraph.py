# SPDX-License-Identifier: Apache-2.0
"""Tenant-bound read-only TaskGraph API over verified revision history."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

from simple_harness.contracts import canonical_json

from ..contracts.htn import GraphStructureBudget, TaskForm
from ..contracts.semantic_base import TypedRef
from ..graph.eligibility import ReadinessReason
from ..graph.notification_contracts import TaskGraphErrorV1, _integer, _text
from ..contracts.models import ContractError
from ..graph.network_codec import decode
from ..graph.structural_diff import diff_documents
from ..orchestrator.hierarchical_dispatch import NetworkView, next_compound_phase
from ..storage.store import Store
from ..storage.taskgraph_store import TaskGraphStore


class TaskGraphReadError(ContractError):
    def __init__(self, error: TaskGraphErrorV1) -> None:
        self.error = error
        self.code = error.code
        super().__init__(error.detail)


def _fail(code: str, detail: str, *, retry: str = "NONE") -> NoReturn:
    raise TaskGraphReadError(TaskGraphErrorV1(origin="SYSTEM", stage="READ", code=code,
        detail=detail[:2000], retry_kind=retry, source_identity=None))


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


CurrentReader = Callable[[str], NetworkView]
EpochReader = Callable[[str], Mapping[str, int]]
ResolutionReader = Callable[[str, int], Sequence[Mapping[str, Any]]]
SourceValidator = Callable[[Any, str], None]
CurrentSourceValidator = Callable[[Any, str, NetworkView], None]


class TaskGraphReadApi:
    def __init__(self, commit: Any, *, tenant_id: str, principal: Any,
                 history: TaskGraphStore, current_reader: CurrentReader,
                 epoch_reader: EpochReader, resolution_reader: ResolutionReader,
                 source_validator: SourceValidator,
                 current_source_validator: CurrentSourceValidator,
                 convergence_diagnostics: Callable[[str, str], Sequence[Mapping[str, Any]]],
                 graph_budget: GraphStructureBudget,
                 seed_reader: Callable[[str], Any] | None = None) -> None:
        if not str(tenant_id).strip() or not isinstance(history, TaskGraphStore):
            raise ValueError("tenant and TaskGraphStore are required")
        if not isinstance(graph_budget, GraphStructureBudget):
            raise ValueError("a bound GraphStructureBudget is required")
        if history.store is not commit.store:
            raise ValueError("history and read API must share the same Store")
        self._commit, self._store = commit, commit.store
        self._tenant, self._principal, self._history = str(tenant_id), principal, history
        self._current_reader, self._epoch_reader = current_reader, epoch_reader
        self._resolution_reader = resolution_reader
        self._source_validator = source_validator
        self._current_source_validator = current_source_validator
        self._budget = graph_budget
        self._convergence_diagnostics = convergence_diagnostics
        self._seed_reader = seed_reader

    def matches_binding(self, commit: Any, tenant_id: str, principal: Any) -> bool:
        return self._commit is commit and self._tenant == tenant_id and self._principal == principal

    def _mission(self, mission_id: str) -> Any:
        gate = getattr(self._commit, "_assurance_root_gate", None)
        if gate is not None:
            gate.require_execution()
        mission = self._store.get_mission(str(mission_id))
        if mission is None or mission.tenant_id != self._tenant:
            _fail("NOT_FOUND", "Mission was not found")
        self._source_validator(self._principal, str(mission_id))
        return mission

    @staticmethod
    def _edges(network: Any) -> list[dict[str, str]]:
        edges: list[dict[str, str]] = []
        adopted = {str(value) for value in network.adopted_instance_ids}
        for method in network.method_instances:
            if str(method.instance_id) not in adopted:
                continue
            for child in method.child_bindings:
                source, target = str(method.effective_goal_occurrence_id), str(child.occurrence_id)
                edges.append({"kind": "refinement", "source": source, "target": target,
                              "identity": _hash([str(method.instance_id), child.slot_key])})
        for item in network.order_constraints:
            edges.append({"kind": "order", "source": str(item.before), "target": str(item.after),
                          "identity": _hash([str(item.before), str(item.after)])})
        for item in network.data_requirements:
            edges.append({"kind": "data", "source": str(item.producer_occurrence),
                          "target": str(item.consumer_occurrence), "identity": item.requirement_id})
        return sorted(edges, key=lambda row: (row["kind"], row["identity"]))

    def _completion_phase(self, mission_id: str, occurrence_id: str,
                          fallback: str) -> tuple[str, list[str]]:
        from ..orchestrator.scoped_content_review import uses_completion_protocol
        from ..storage.htn_store import HtnStore
        if not uses_completion_protocol(self._store, mission_id):
            return fallback, []
        if HtnStore(self._store).active_plan_revision(mission_id) is None:
            return "AWAITING_INITIAL_PLAN", ["尚无已提交计划；初始目标不具有执行或完成许可。"]
        from ..orchestrator.completion_status import read_current_effect, read_occurrence_completion
        from ..orchestrator.operation_completion import OperationCompletionError
        try:
            status = read_occurrence_completion(self._store, mission_id, occurrence_id)
        except OperationCompletionError as error:
            if error.code not in {"OP_COMPLETION_SCOPE_UNRESOLVED", "OP_REQUIREMENT_MAPPING_MISSING"}:
                raise
            return "AWAITING_COMPLETION_MAPPING", ["当前完成要求或归属尚未确认：" + error.code]
        details = ["内容验收：" + ("已通过" if status.content_ready else "未通过"),
                   "效果验收：" + ("已通过" if status.effects_ready else "未通过")]
        if len(status.scope.required_effect_keys) > 28:
            _fail("BOUND_REACHED", "completion explanation exceeds its public bound")
        pending_states = []
        for key in status.scope.required_effect_keys:
            effect = read_current_effect(self._store, mission_id, status.scope.spec_hash, key)
            state = _text(effect["state"], "effect state")
            details.append(key + ": " + state)
            if effect["complete"] is not True:
                pending_states.append(state)
        if status.complete:
            return "COMPLETED", details
        # Compound content review can wait on an already executed effect. Its
        # content_ready is still false, but that must not hide the pending review.
        if "AWAITING_OUTCOME_REVIEW" in pending_states:
            return "AWAITING_OUTCOME_REVIEW", details
        if "RECONCILIATION_REQUIRED" in pending_states:
            return "RECONCILIATION_REQUIRED", details
        if status.preparation_ready or status.content_ready:
            if pending_states:
                return "WAITING_EFFECT", details
            return "WAITING_FORMAL_CONTENT_REVIEW", details
        if status.effects_ready and status.scope.required_effect_keys:
            return "WAITING_FORMAL_CONTENT_REVIEW", details
        return fallback, details

    def _require_enabled(self, mission_id: str) -> None:
        """A Mission that never enabled the TaskGraph kernel has no graph to read.

        2026-09-25 UI 全量点击: reading one used to fail as POLICY_UNAVAILABLE /
        HISTORY_INTEGRITY (an operator-repair corruption signal) although nothing
        was wrong.  Say so plainly instead; once enabled, the strict integrity
        checks below still apply unchanged.
        """
        from ..orchestrator.taskgraph_dispatch import taskgraph_enabled
        if not taskgraph_enabled(self._store, mission_id):
            _fail("NOT_ENABLED", "此任务未启用执行图")

    def snapshot(self, mission_id: str, *, revision: int | None = None) -> dict[str, Any]:
        return self._snapshot(mission_id, revision=revision)[0]

    def _snapshot(self, mission_id: str, *, revision: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        explanation_sources: dict[str, Any] = {}
        with self._store.read_view() as connection:
            self._mission(mission_id)
            self._require_enabled(mission_id)
            historical = revision is not None
            seed = None
            if revision is None:
                rows = connection.execute("SELECT revision FROM plan_revisions WHERE mission_id=? "
                    "AND state='ACTIVE'", (mission_id,)).fetchall()
                if not rows:
                    if self._seed_reader is None:
                        _fail("SOURCE_UNAVAILABLE", "original root seed reader is required", retry="REQUERY")
                    from ..orchestrator.taskgraph_sources import SeedStructuralReadContext
                    seed = self._seed_reader(mission_id)
                    if not isinstance(seed, SeedStructuralReadContext) or seed.document.mission_id != mission_id:
                        _fail("SOURCE_UNAVAILABLE", "original root seed identity is invalid", retry="REQUERY")
                    selected = 0
                elif len(rows) == 1:
                    selected = int(rows[0][0])
                else:
                    _fail("GRAPH_INTEGRITY", "multiple ACTIVE revisions are invalid", retry="OPERATOR_REPAIR")
            else:
                if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
                    _fail("INVALID_REVISION", "revision must be a nonnegative integer")
                selected = revision
            if seed is None:
                record = self._history.read_revision(mission_id, selected).record
                document, manifest_hash = record.document, record.manifest_hash
            else:
                document, manifest_hash = seed.document, seed.token.manifest_hash
            network = decode(document.to_json()).snapshot
            if not historical:
                from ..graph.taskgraph_validation import validate_taskgraph_structure
                from ..orchestrator.taskgraph_policy import read_installed_graph_policy
                policy_budget = GraphStructureBudget.from_json(
                    read_installed_graph_policy(self._store, mission_id).to_json()["graph_structure_budget"])
                structure_report = validate_taskgraph_structure(document, policy_budget)
                if not structure_report.ok:
                    from ..graph.projection_validation import ProblemKind
                    if any(problem.kind is ProblemKind.BOUND_REACHED for problem in structure_report.problems):
                        _fail("BOUND_REACHED", "Current execution graph exceeds its installed structure budget")
                    _fail("GRAPH_INTEGRITY", "Current execution graph failed complete structure checks",
                          retry="OPERATOR_REPAIR")
            edges = self._edges(network)
            projection = network.execution_projection()
            widest = max((len(targets) for targets in projection.successors().values()), default=0)
            if (len(projection.nodes) > self._budget.max_nodes
                    or len(projection.edges) > self._budget.max_edges
                    or widest > self._budget.max_fan_out
                    or len(network.occurrences) > 4096 or len(edges) > 8192):
                _fail("BOUND_REACHED", "FULL_BOUNDED_SNAPSHOT exceeds the configured graph budget")
            through = int(connection.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?",
                                             (mission_id,)).fetchone()[0])
            epochs = [] if historical else [
                {"scope_id": key, "epoch": value}
                for key, value in sorted(self._epoch_reader(mission_id).items())
            ]
            if len(epochs) > 256 or any(type(row["epoch"]) is not int or row["epoch"] < 0 for row in epochs):
                _fail("SOURCE_UNAVAILABLE", "validity epoch source is incomplete", retry="REQUERY")
            nodes: list[dict[str, Any]]
            if historical:
                nodes = [{"occurrence_id": str(item.occurrence_id), "task_id": str(item.task_id),
                    "obligation_id": str(item.obligation_id), "form": str(item.form),
                    "contract_revision": int(network.binding_for_occurrence(item.occurrence_id).contract_revision),
                    "dispatch_generation": int(network.binding_for_occurrence(item.occurrence_id).dispatch_generation),
                    "phase": "HISTORY_ONLY", "readiness": "NOT_SELECTED",
                    "reason_codes": ["HISTORICAL_VIEW_NON_EXECUTABLE"]}
                    for item in network.occurrences]
                planning: list[str] = []
                execution: list[str] = []
                roots = list(self._resolution_reader(mission_id, selected))
                mode = "HISTORICAL_STRUCTURE"
            else:
                current = self._current_reader(mission_id)
                if int(current.network.plan_revision) != selected:
                    _fail("SOURCE_CHANGED", "current typed read selected another revision", retry="REQUERY")
                structural_fields = ("mission_id", "plan_revision", "occurrences", "method_instances",
                                     "adopted_instance_ids", "root_occurrence_ids", "order_constraints",
                                     "data_requirements", "typed_edges", "obligation_coverage", "required_obligations")
                if any(getattr(current.network, name) != getattr(network, name) for name in structural_fields):
                    _fail("SOURCE_CHANGED", "current structure differs from the verified revision", retry="REQUERY")
                # Binding controls are current facts and may advance independently;
                # the installed execution-source reader must verify those transitions.
                expected_ids = {item.occurrence_id for item in network.occurrences}
                if set(current.views) != expected_ids or set(current.reports) != expected_ids:
                    _fail("SOURCE_UNAVAILABLE", "current typed read omits or adds occurrences", retry="REQUERY")
                self._current_source_validator(self._principal, mission_id, current)
                nodes = []
                for item in network.occurrences:
                    view, report = current.views[item.occurrence_id], current.reports[item.occurrence_id]
                    binding = view.binding
                    if binding is None:
                        _fail("GRAPH_INTEGRITY", "current TaskView lacks a semantic binding")
                    phase = str(next_compound_phase(item, current.network, report,
                        child_outcomes=current.outcomes, resolved=item.occurrence_id in current.resolved)) \
                        if item.form is TaskForm.COMPOUND else _text(view.legacy_status, "task status")
                    phase, completion_details = self._completion_phase(mission_id, str(item.occurrence_id), phase)
                    nodes.append({"occurrence_id": str(item.occurrence_id), "task_id": str(item.task_id),
                        "obligation_id": str(item.obligation_id), "form": str(item.form),
                        "contract_revision": int(binding.contract_revision),
                        "dispatch_generation": int(binding.dispatch_generation),
                        "phase": phase,
                        "readiness": str(ReadinessReason(report.reason)),
                        "reason_codes": list(report.detail_codes)})
                    if (str(report.mission_id) != mission_id or int(report.plan_revision) != selected
                            or report.task_id != item.task_id or report.occurrence_id != item.occurrence_id
                            or view.task_id != item.task_id or view.occurrence_id != item.occurrence_id):
                        _fail("SOURCE_CHANGED", "readiness identity disagrees with current occurrence", retry="REQUERY")
                    explanation_sources[str(item.occurrence_id)] = (report.read_set, completion_details)
                planning = sorted(map(str, current.planning_frontier.occurrences))
                execution = sorted(map(str, current.execution_frontier.occurrences))
                if seed is None:
                    roots = list(self._resolution_reader(mission_id, selected))
                else:
                    from ..storage.htn_store import HtnStore
                    if HtnStore(self._store).list_goal_resolutions(mission_id):
                        _fail("GRAPH_INTEGRITY", "uncommitted root seed has a resolution")
                    roots = []
                    if execution:
                        _fail("GRAPH_INTEGRITY", "uncommitted root seed cannot authorize execution")
                mode = "CURRENT"
            if len(roots) > 256 or any(len(node["reason_codes"]) > 32 for node in nodes):
                _fail("BOUND_REACHED", "snapshot source arrays exceed their public bound")
            for root in roots:
                reference = TypedRef.from_json(dict(root))
                if str(reference.kind) != "resolution":
                    _fail("SOURCE_UNAVAILABLE", "root source must reference a real resolution")
            for epoch in epochs:
                _text(epoch["scope_id"], "scope_id")
                _integer(epoch["epoch"], "epoch")
            _integer(through, "through_seq")
            body: dict[str, Any] = {"schema_version": 1, "mission_id": mission_id,
                "read_token": {"plan_revision": selected, "through_seq": through,
                    "validity_epochs": epochs, "snapshot_hash": "", "manifest_hash": manifest_hash},
                "nodes": sorted(nodes, key=lambda row: row["occurrence_id"]), "edges": edges,
                "planning_frontier": planning, "execution_frontier": execution,
                "root_resolution_refs": roots, "next_cursor": None, "complete": True,
                "view_mode": mode}
            tokenless = {**body, "read_token": {key: value for key, value in body["read_token"].items()
                                                if key != "snapshot_hash"}}
            body["read_token"]["snapshot_hash"] = _hash(tokenless)
            return body, explanation_sources

    def why_not_ready(self, mission_id: str, occurrence_id: str) -> dict[str, Any]:
        view, sources = self._snapshot(mission_id)
        node = next((item for item in view["nodes"] if item["occurrence_id"] == occurrence_id), None)
        if node is None:
            _fail("NOT_FOUND", "occurrence was not found")
        reads, completion_details = sources[occurrence_id]
        refs = [{"kind": "network_document", "id": mission_id,
                 "revision": view["read_token"]["plan_revision"],
                 "content_hash": view["read_token"]["manifest_hash"]}]
        for channel in ("goal_revisions", "method_revisions", "observation_revisions",
                        "acceptance_revisions", "obligation_revisions", "authority_revisions"):
            for item in getattr(reads, channel):
                ref = {"kind": str(item.kind), "id": str(item.id),
                       "revision": int(item.semantic_revision), "content_hash": item.content_hash}
                if ref not in refs:
                    refs.append(ref)
        if len(refs) > 64:
            _fail("BOUND_REACHED", "explanation source references exceed their public bound")
        return {"schema_version": 1, "mission_id": mission_id, "occurrence_id": occurrence_id,
                "read_token": view["read_token"], "readiness": node["readiness"],
                "reason_codes": node["reason_codes"], "source_refs": refs,
                "details": [f"phase={node['phase']}", *completion_details]}

    def diff(self, mission_id: str, from_revision: int, to_revision: int) -> dict[str, Any]:
        with self._store.read_view():
            self._mission(mission_id)
            self._require_enabled(mission_id)
            before = self._history.read_revision(mission_id, from_revision).record.document
            after = self._history.read_revision(mission_id, to_revision).record.document
            return diff_documents(before, after).to_json()

    def convergence(self, mission_id: str) -> dict[str, Any]:
        with self._store.read_view() as connection:
            self._mission(mission_id)
            self._require_enabled(mission_id)
            through = int(connection.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?",
                                             (mission_id,)).fetchone()[0])
            jobs = []
            for row in connection.execute("SELECT * FROM taskgraph_convergence_jobs WHERE mission_id=? ORDER BY job_id", (mission_id,)):
                targets = [dict(item) for item in connection.execute(
                    "SELECT occurrence_id,task_id,expected_generation,target_kind FROM "
                    "taskgraph_convergence_targets WHERE job_id=? ORDER BY occurrence_id", (row["job_id"],))]
                diagnostics = [dict(ref) for ref in self._convergence_diagnostics(mission_id, row["job_id"])]
                if len(diagnostics) > 64:
                    _fail("BOUND_REACHED", "convergence diagnostics exceed their public bound")
                for ref in diagnostics:
                    from ..graph.notification_contracts import FollowupCauseRef
                    FollowupCauseRef.from_json(ref)
                jobs.append({"job_id": row["job_id"], "decision_id": row["decision_id"],
                    "source_revision": row["source_revision"], "candidate_hash": row["candidate_hash"],
                    "impact_hash": row["impact_hash"], "state": row["state"],
                    "row_version": row["row_version"], "targets": targets, "diagnostic_refs": diagnostics})
            if len(jobs) > 4096 or any(len(job["targets"]) > 4096 for job in jobs):
                _fail("BOUND_REACHED", "convergence view exceeds its public bound")
            return {"schema_version": 1, "mission_id": mission_id, "through_seq": through,
                    "jobs": jobs, "complete": True}


__all__ = ["TaskGraphReadApi", "TaskGraphReadError"]
