# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static structural-conformance fixture for Host main-model analysis executors."""

from simple_harness import (
    EMPTY_WORKSPACE_BINDING_ROOT_SET_DIGEST,
    CanonicalWorkspaceRoot,
    ContextAssemblyDecision,
    ContextFragment,
    ContextFragmentBindingV2,
    ContextRouteReceipt,
    ContextRouteState,
    DisclosureContext,
    EpisodeMemoryPayload,
    EvidenceSpanRef,
    FilesystemIdentity,
    FilesystemIdentityKind,
    HostIssuedRunBindingModeSnapshot,
    LongTermMemoryType,
    ManualWorkspaceBindingAuthorizationReceipt,
    ManualWorkspaceBindingChallenge,
    MemoryAnalysisDeliveryAuthorityPort,
    MemoryAnalysisExecutorPort,
    MemoryAnalysisRequest,
    MemoryAnalysisResultEnvelope,
    MemoryMutationPlan,
    ProcedureMemoryPayload,
    ProspectiveMemoryPayload,
    RecallContext,
    RecallContextUseAuthorizationRequestV1,
    RecallContextUseReceiptV1,
    RecallDecisionV4,
    RecallPlan,
    RecallResultPageRequestV1,
    RecallSelectedItemV4,
    RecallSelectorDomain,
    RunBindingModeSnapshotRequest,
    RunContextAuthorityPort,
    RunContextAuthorityRequest,
    RunContextSnapshot,
    RuntimeDecisionSinkPort,
    SanitizedEvidenceEnvelope,
    SemanticMemoryPayload,
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
    TypedRecallResultV1,
    WorkspaceBindingAuthorityGrant,
    WorkspaceBindingAuthorityPort,
    WorkspaceBindingProposal,
    WorkspaceBindingSetReceipt,
    workspace_binding_root_set_digest,
)


class StructuralMemoryAnalysisExecutor:
    async def analyze_memory(self, request: MemoryAnalysisRequest) -> MemoryAnalysisResultEnvelope:
        raise NotImplementedError(request)


class StructuralMemoryAnalysisDeliveryAuthority:
    async def verify_analysis_delivery(
        self, request: MemoryAnalysisRequest, envelope: MemoryAnalysisResultEnvelope
    ) -> None:
        raise NotImplementedError(request, envelope)


class StructuralRunContextAuthority:
    async def prepare_snapshot(self, request: RunContextAuthorityRequest) -> RunContextSnapshot:
        raise NotImplementedError(request)


class StructuralTaskExecutionAuthority:
    async def issue_envelope(self, request: TaskExecutionEnvelopeRequest) -> TaskExecutionEnvelope:
        raise NotImplementedError(request)


class StructuralWorkspaceBindingAuthority:
    async def verify_manual_authorization(
        self,
        proposal: WorkspaceBindingProposal,
        challenge: ManualWorkspaceBindingChallenge,
        receipt: ManualWorkspaceBindingAuthorizationReceipt,
    ) -> WorkspaceBindingAuthorityGrant:
        raise NotImplementedError(proposal, challenge, receipt)

    async def issue_run_binding_mode_snapshot(
        self, request: RunBindingModeSnapshotRequest
    ) -> HostIssuedRunBindingModeSnapshot:
        raise NotImplementedError(request)

    async def authorize_auto_binding(
        self,
        proposal: WorkspaceBindingProposal,
        snapshot: HostIssuedRunBindingModeSnapshot,
    ) -> WorkspaceBindingAuthorityGrant:
        raise NotImplementedError(proposal, snapshot)

    async def verify_binding_grant(
        self, proposal: WorkspaceBindingProposal, grant: WorkspaceBindingAuthorityGrant
    ) -> None:
        raise NotImplementedError(proposal, grant)


def accepts_executor(value: MemoryAnalysisExecutorPort) -> MemoryAnalysisExecutorPort:
    return value


STRUCTURAL_EXECUTOR: MemoryAnalysisExecutorPort = accepts_executor(
    StructuralMemoryAnalysisExecutor()
)
STRUCTURAL_ANALYSIS_DELIVERY_AUTHORITY: MemoryAnalysisDeliveryAuthorityPort = (
    StructuralMemoryAnalysisDeliveryAuthority()
)
STRUCTURAL_CONTEXT_AUTHORITY: RunContextAuthorityPort = StructuralRunContextAuthority()
STRUCTURAL_TASK_AUTHORITY: TaskExecutionAuthorityPort = StructuralTaskExecutionAuthority()
STRUCTURAL_WORKSPACE_BINDING_AUTHORITY: WorkspaceBindingAuthorityPort = (
    StructuralWorkspaceBindingAuthority()
)
DISCLOSURE_TYPE: type[DisclosureContext] = DisclosureContext
CONTEXT_FRAGMENT_TYPE: type[ContextFragment] = ContextFragment
CONTEXT_DECISION_TYPE: type[ContextAssemblyDecision] = ContextAssemblyDecision
EVIDENCE_TYPE: type[SanitizedEvidenceEnvelope] = SanitizedEvidenceEnvelope
RECALL_PLAN_TYPE: type[RecallPlan] = RecallPlan
RECALL_CONTEXT_TYPE: type[RecallContext] = RecallContext
RECALL_DECISION_TYPE: type[RecallDecisionV4] = RecallDecisionV4
RECALL_SELECTED_ITEM_TYPE: type[RecallSelectedItemV4] = RecallSelectedItemV4
RECALL_RESULT_TYPE: type[TypedRecallResultV1] = TypedRecallResultV1
RECALL_PAGE_REQUEST_TYPE: type[RecallResultPageRequestV1] = RecallResultPageRequestV1
RECALL_USE_REQUEST_TYPE: type[RecallContextUseAuthorizationRequestV1] = (
    RecallContextUseAuthorizationRequestV1
)
RECALL_USE_RECEIPT_TYPE: type[RecallContextUseReceiptV1] = RecallContextUseReceiptV1
CONTEXT_FRAGMENT_BINDING_TYPE: type[ContextFragmentBindingV2] = ContextFragmentBindingV2
RECALL_SELECTOR_TYPE: type[RecallSelectorDomain] = RecallSelectorDomain
MEMORY_MUTATION_PLAN_TYPE: type[MemoryMutationPlan] = MemoryMutationPlan
EPISODE_PAYLOAD_TYPE: type[EpisodeMemoryPayload] = EpisodeMemoryPayload
SEMANTIC_PAYLOAD_TYPE: type[SemanticMemoryPayload] = SemanticMemoryPayload
PROCEDURE_PAYLOAD_TYPE: type[ProcedureMemoryPayload] = ProcedureMemoryPayload
PROSPECTIVE_PAYLOAD_TYPE: type[ProspectiveMemoryPayload] = ProspectiveMemoryPayload
EVIDENCE_SPAN_TYPE: type[EvidenceSpanRef] = EvidenceSpanRef
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
FILESYSTEM_IDENTITY_TYPE: type[FilesystemIdentity] = FilesystemIdentity
FILESYSTEM_IDENTITY_KIND: FilesystemIdentityKind = FilesystemIdentityKind.POSIX_INODE
CANONICAL_WORKSPACE_ROOT_TYPE: type[CanonicalWorkspaceRoot] = CanonicalWorkspaceRoot
WORKSPACE_BINDING_PROPOSAL_TYPE: type[WorkspaceBindingProposal] = WorkspaceBindingProposal
WORKSPACE_BINDING_SET_RECEIPT_TYPE: type[WorkspaceBindingSetReceipt] = WorkspaceBindingSetReceipt
EMPTY_ROOT_SET_DIGEST: str = EMPTY_WORKSPACE_BINDING_ROOT_SET_DIGEST
ROOT_SET_DIGEST: str = workspace_binding_root_set_digest(("a" * 64,))
