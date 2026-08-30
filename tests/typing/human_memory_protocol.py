# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static structural-conformance fixture for Host main-model analysis executors."""

from simple_harness import (
    ContextAssemblyDecision,
    ContextFragment,
    ContextRouteReceipt,
    ContextRouteState,
    DisclosureContext,
    LongTermMemoryType,
    MemoryAnalysisExecutorPort,
    MemoryAnalysisRequest,
    MemoryAnalysisResult,
    RecallPlan,
    RunContextAuthorityPort,
    RunContextAuthorityRequest,
    RunContextSnapshot,
    RuntimeDecisionSinkPort,
    SanitizedEvidenceEnvelope,
    TaskExecutionAuthorityPort,
    TaskExecutionEnvelope,
    TaskExecutionEnvelopeRequest,
    TaskScopeMutationPlan,
    TaskScopeProposal,
    TaskScopeRoute,
    ToolEffectClass,
    ToolExecutionPolicy,
    ToolRouteRequirement,
    ToolTaskScopeRequirement,
)


class StructuralMemoryAnalysisExecutor:
    async def analyze_memory(self, request: MemoryAnalysisRequest) -> MemoryAnalysisResult:
        raise NotImplementedError(request)


class StructuralRunContextAuthority:
    async def prepare_snapshot(
        self, request: RunContextAuthorityRequest
    ) -> RunContextSnapshot:
        raise NotImplementedError(request)


class StructuralTaskExecutionAuthority:
    async def issue_envelope(
        self, request: TaskExecutionEnvelopeRequest
    ) -> TaskExecutionEnvelope:
        raise NotImplementedError(request)


def accepts_executor(value: MemoryAnalysisExecutorPort) -> MemoryAnalysisExecutorPort:
    return value


STRUCTURAL_EXECUTOR: MemoryAnalysisExecutorPort = accepts_executor(
    StructuralMemoryAnalysisExecutor()
)
STRUCTURAL_CONTEXT_AUTHORITY: RunContextAuthorityPort = StructuralRunContextAuthority()
STRUCTURAL_TASK_AUTHORITY: TaskExecutionAuthorityPort = StructuralTaskExecutionAuthority()
DISCLOSURE_TYPE: type[DisclosureContext] = DisclosureContext
CONTEXT_FRAGMENT_TYPE: type[ContextFragment] = ContextFragment
CONTEXT_DECISION_TYPE: type[ContextAssemblyDecision] = ContextAssemblyDecision
EVIDENCE_TYPE: type[SanitizedEvidenceEnvelope] = SanitizedEvidenceEnvelope
RECALL_PLAN_TYPE: type[RecallPlan] = RecallPlan
MEMORY_TYPES: tuple[LongTermMemoryType, ...] = tuple(LongTermMemoryType)
TASK_PROPOSAL_TYPE: type[TaskScopeProposal] = TaskScopeProposal
TASK_MUTATION_TYPE: type[TaskScopeMutationPlan] = TaskScopeMutationPlan
CREATE_ROUTE: TaskScopeRoute = TaskScopeRoute.CREATE_NEW
CONTEXT_ROUTE_RECEIPT_TYPE: type[ContextRouteReceipt] = ContextRouteReceipt
CONTEXT_ROUTE_STATE: ContextRouteState = ContextRouteState.ROUTED_STANDALONE
RUN_CONTEXT_AUTHORITY_TYPE: type[RunContextAuthorityPort] = RunContextAuthorityPort
RUN_CONTEXT_REQUEST_TYPE: type[RunContextAuthorityRequest] = RunContextAuthorityRequest
RUN_CONTEXT_SNAPSHOT_TYPE: type[RunContextSnapshot] = RunContextSnapshot
DECISION_SINK_TYPE: type[RuntimeDecisionSinkPort] = RuntimeDecisionSinkPort
TASK_EXECUTION_AUTHORITY_TYPE: type[TaskExecutionAuthorityPort] = TaskExecutionAuthorityPort
TASK_EXECUTION_REQUEST_TYPE: type[TaskExecutionEnvelopeRequest] = TaskExecutionEnvelopeRequest
TASK_EXECUTION_ENVELOPE_TYPE: type[TaskExecutionEnvelope] = TaskExecutionEnvelope
TOOL_EFFECT_CLASS: ToolEffectClass = ToolEffectClass.PROJECT_EFFECT
TOOL_EXECUTION_POLICY_TYPE: type[ToolExecutionPolicy] = ToolExecutionPolicy
TOOL_ROUTE_REQUIREMENT: ToolRouteRequirement = ToolRouteRequirement.REQUIRED
TOOL_TASK_SCOPE_REQUIREMENT: ToolTaskScopeRequirement = ToolTaskScopeRequirement.REQUIRED
