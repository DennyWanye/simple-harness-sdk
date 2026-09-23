# SPDX-License-Identifier: Apache-2.0
"""Fixed deployment installation of the TaskGraph collaborators as one unit.

All authoritative ports are mandatory. This installer does not create grants,
APPLIED checks, baseline proofs or deployment acceptance, and enables no Mission.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..api.taskgraph import TaskGraphReadApi
from ..contracts.htn import GraphStructureBudget
from ..governance.permissions import Principal
from ..governance.budgets import BudgetError
from ..storage.taskgraph_store import TaskGraphStore
from ..storage.taskgraph_followups import TaskGraphFollowupStore
from ..storage.taskgraph_convergence import TaskGraphConvergenceStore
from .taskgraph_convergence import TaskGraphConvergence
from .taskgraph_convergence_authority import VerifiedConvergenceAuthority
from .taskgraph_dispatch import TaskGraphDispatchBinding
from .taskgraph_materialization import TaskGraphExecutionGuard
from .taskgraph_notifications import TaskGraphNotifications
from .taskgraph_preview import TaskGraphPreviewService
from .taskgraph_plan_sources import TaskGraphPlanSourceReader
from .taskgraph_execution_policy import TaskGraphExecutionImports
from .taskgraph_runtime_imports import TaskGraphRuntimeImports
from .taskgraph_policy import TaskGraphPolicyService
from .taskgraph_policy_sources import DeploymentAcceptanceReader, StoreTaskGraphPolicyAuthority
from .taskgraph_resolutions import TaskGraphResolutionReader
from .taskgraph_baseline import TaskGraphBaselineCapture
from .taskgraph_baseline_proof import TaskGraphBaselineProof
from .taskgraph_operator import TaskGraphOperatorGuard, TaskGraphOperatorService
from .taskgraph_runtime import TaskGraphRuntimeCommands
from .taskgraph_settlement import TaskGraphSettlementReader
from .taskgraph_sources import TaskGraphSources
from .taskgraph_execution_sources import TaskGraphLocalExecutionReader
from ..runtime.planning_operations import SourceUnavailable

@dataclass(frozen=True, slots=True, kw_only=True)
class TaskGraphDeploymentPorts:
    tenant_id: str
    principal: Principal
    deployment_acceptance: DeploymentAcceptanceReader
    graph_budget: GraphStructureBudget


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledTaskGraph:
    reads: TaskGraphReadApi
    policy: TaskGraphPolicyService
    operator: TaskGraphOperatorService
    notifications: TaskGraphNotifications
    dispatch: TaskGraphDispatchBinding
    convergence: TaskGraphConvergence
    local_sources: TaskGraphLocalExecutionReader
    preview: TaskGraphPreviewService
    execution_sources: TaskGraphPlanSourceReader


def install_taskgraph(orchestrator: Any, ports: TaskGraphDeploymentPorts) -> InstalledTaskGraph:
    """Install after hierarchical setup, before recovery starts the SDK runtime."""
    if not isinstance(ports, TaskGraphDeploymentPorts):
        raise ValueError("TaskGraph requires fixed deployment ports")
    hierarchy, commit, store = orchestrator._hierarchical, orchestrator.commit, orchestrator.store
    if hierarchy is None or hierarchy.store is not store or hierarchy.commit is not commit:
        raise ValueError("TaskGraph requires the original hierarchical runtime")
    if (not isinstance(ports.principal, Principal) or not ports.tenant_id.strip()
            or not isinstance(ports.graph_budget, GraphStructureBudget)):
        raise ValueError("TaskGraph requires an authenticated tenant and installed structure budget")
    if (orchestrator._taskgraph_dispatch_setup is not None or commit._taskgraph_dispatch is not None or commit._taskgraph_participant_factory is not None
            or orchestrator._taskgraph_notifications is not None or orchestrator._taskgraph_read_apis
            or orchestrator._taskgraph_operator is not None
            or orchestrator._taskgraph_policy is not None
            or hierarchy._taskgraph_settlement_reader is not None or hierarchy._taskgraph_history is not None
            or hierarchy._taskgraph_preview is not None):
        raise ValueError("TaskGraph runtime is already or partially installed")
    if not callable(ports.deployment_acceptance):
        raise ValueError("TaskGraph actual deployment acceptance reader is missing")
    history = TaskGraphStore(store)

    def dispatch_for(mission_id: str) -> Any:
        dispatch = orchestrator._dispatch_for(mission_id)
        if dispatch is None or dispatch.store is not store or dispatch.commit is not commit:
            raise SourceUnavailable("taskgraph_mission_dispatcher_unavailable")
        return dispatch

    def validate_read(principal: Any, mission_id: str) -> None:
        # This uses the same original Mission ownership rule as MissionControlV1.
        # Historical reads do not require a currently active planning delegation.
        if principal != ports.principal:
            raise SourceUnavailable("taskgraph_read_principal_mismatch")
        mission = store.get_mission(mission_id)
        if mission is None or mission.tenant_id != ports.tenant_id:
            raise SourceUnavailable("mission_not_found")

    def validate_sources(principal: Any, mission_id: str, view: Any) -> None:
        with store.read_view():
            validate_read(principal, mission_id)
            local = plan_sources.read_execution(mission_id).local
            if (local.view.network != view.network or local.view.witnesses != view.witnesses
                    or local.view.starts != view.starts or local.view.licences != view.licences
                    or local.view.accepted != view.accepted):
                raise SourceUnavailable("taskgraph_execution_sources_changed")

    def validate_current(mission_id: str) -> None:
        with store.read_view():
            validate_read(ports.principal, mission_id)
            validate_sources(ports.principal, mission_id, dispatch_for(mission_id).read(mission_id))

    def recheck_execution(original_store: Any, view: Any, admission: Any, manifest: Any,
                          config: Any, inputs: Any, input_hash: str) -> None:
        if original_store is not store:
            raise SourceUnavailable("taskgraph_execution_store_mismatch")
        execution = plan_sources.read_execution(str(view.network.mission_id), materializing=True)
        local = execution.local
        if local.view.network != view.network:
            raise SourceUnavailable("taskgraph_execution_sources_changed")
        execution_imports.validate_attempt(store, str(admission.task_id), execution.execution_policy, config)
        execution_guard(store, view, admission, manifest, config, inputs, input_hash)

    def recheck_settlement(original_store: Any, subject_id: str, mission_id: str) -> None:
        if original_store is not store or not store.connection.in_transaction:
            raise SourceUnavailable("taskgraph_settlement_transaction_required")
        intent = store.get_intent_for_subject(subject_id)
        try:
            if intent is None:
                from .taskgraph_action_settlement import require_action_settlement
                require_action_settlement(orchestrator, subject_id, mission_id)
                return
            if intent.mission_id != mission_id:
                raise SourceUnavailable("taskgraph_settlement_intent_mismatch")
            settled = runtime_imports.read_subject(intent).physical_settled
        except SourceUnavailable as error:
            raise BudgetError("TASKGRAPH_SETTLEMENT_SOURCE_UNAVAILABLE") from error
        if not settled:
            raise BudgetError("TASKGRAPH_SETTLEMENT_PHYSICAL_WORK_UNRESOLVED")

    # Construct everything before changing runtime fields. A rejected constructor
    # leaves the original deployment with none of this assembly installed.
    local_sources = TaskGraphLocalExecutionReader(TaskGraphSources(store, tenant_id=ports.tenant_id,
        principal=ports.principal, history=history), dispatch_for)
    runtime_imports = TaskGraphRuntimeImports(orchestrator)
    execution_guard = TaskGraphExecutionGuard(orchestrator)
    execution_imports = TaskGraphExecutionImports(orchestrator, runtime_imports=runtime_imports)
    plan_sources = TaskGraphPlanSourceReader(local_sources, imports=execution_imports)
    dispatch = TaskGraphDispatchBinding(store, hierarchical=dispatch_for, history=history,
        recheck_execution=recheck_execution, recheck_handoff=execution_imports.validate_handoff,
        recheck_settlement=recheck_settlement)
    operator_guard = TaskGraphOperatorGuard(store, tenant_id=ports.tenant_id,
        principal=ports.principal, validate_read=validate_read)
    jobs = TaskGraphConvergenceStore(store, authority=VerifiedConvergenceAuthority(orchestrator,
        history=history, sources=plan_sources, operator=operator_guard))
    preview = TaskGraphPreviewService(store, commit=commit, history=history, read_sources=plan_sources, convergence=jobs)
    convergence = TaskGraphConvergence(jobs, runtime=TaskGraphRuntimeCommands(orchestrator,
        jobs=jobs, history=history))
    settlement = TaskGraphSettlementReader(orchestrator, history=history)
    notifications = TaskGraphNotifications(orchestrator, history=history,
        convergence=convergence, validate_current=validate_current)
    operator = TaskGraphOperatorService(operator_guard, jobs=jobs, notifications=notifications, sources=plan_sources)
    reads = TaskGraphReadApi(commit, tenant_id=ports.tenant_id, principal=ports.principal,
        history=history, current_reader=lambda mission_id: dispatch_for(mission_id).read(mission_id),
        epoch_reader=lambda mission_id: dispatch_for(mission_id).scope_epochs(mission_id),
        resolution_reader=TaskGraphResolutionReader(history), source_validator=validate_read,
        seed_reader=lambda mission_id: local_sources.read(mission_id).structure,
        current_source_validator=validate_sources, graph_budget=ports.graph_budget,
        convergence_diagnostics=TaskGraphFollowupStore(store).convergence_diagnostics)
    policy_authority = StoreTaskGraphPolicyAuthority(orchestrator, tenant_id=ports.tenant_id,
        principal=ports.principal, graph_budget=ports.graph_budget, validate_read=validate_read,
        deployment_acceptance=ports.deployment_acceptance)
    policy = TaskGraphPolicyService(store, tenant_id=ports.tenant_id, principal=ports.principal,
        authority=policy_authority, capture_baseline=TaskGraphBaselineCapture(dispatch_for, history=history,
            prove_quiescence=TaskGraphBaselineProof(orchestrator)).capture,
        validate_read=validate_read)
    installed = InstalledTaskGraph(reads=reads, policy=policy, operator=operator, notifications=notifications,
                                   dispatch=dispatch, convergence=convergence, local_sources=local_sources, preview=preview, execution_sources=plan_sources)
    commit._taskgraph_dispatch = dispatch
    commit._taskgraph_participant_factory = preview.participant
    orchestrator._taskgraph_notifications = notifications
    orchestrator._taskgraph_read_apis = [reads]
    orchestrator._taskgraph_operator = operator
    orchestrator._taskgraph_policy = policy
    def attach_dispatch(dispatch: Any) -> None:
        if dispatch.store is not store or dispatch.commit is not commit:
            raise SourceUnavailable("taskgraph_mission_dispatcher_store_mismatch")
        dispatch._taskgraph_settlement_reader = settlement
        dispatch._taskgraph_history = history
        dispatch._taskgraph_preview = preview
    orchestrator._taskgraph_dispatch_setup = attach_dispatch
    attach_dispatch(hierarchy)
    for dispatch in orchestrator._mission_dispatches.values():
        attach_dispatch(dispatch)
    return installed
