# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static structural-conformance fixture for Host main-model analysis executors."""

from simple_harness import (
    ContextAssemblyDecision,
    ContextFragment,
    DisclosureContext,
    LongTermMemoryType,
    MemoryAnalysisExecutorPort,
    MemoryAnalysisRequest,
    MemoryAnalysisResult,
    RecallPlan,
    SanitizedEvidenceEnvelope,
    TaskScopeMutationPlan,
    TaskScopeProposal,
    TaskScopeRoute,
)


class StructuralMemoryAnalysisExecutor:
    async def analyze_memory(self, request: MemoryAnalysisRequest) -> MemoryAnalysisResult:
        raise NotImplementedError(request)


def accepts_executor(value: MemoryAnalysisExecutorPort) -> MemoryAnalysisExecutorPort:
    return value


STRUCTURAL_EXECUTOR: MemoryAnalysisExecutorPort = accepts_executor(
    StructuralMemoryAnalysisExecutor()
)
DISCLOSURE_TYPE: type[DisclosureContext] = DisclosureContext
CONTEXT_FRAGMENT_TYPE: type[ContextFragment] = ContextFragment
CONTEXT_DECISION_TYPE: type[ContextAssemblyDecision] = ContextAssemblyDecision
EVIDENCE_TYPE: type[SanitizedEvidenceEnvelope] = SanitizedEvidenceEnvelope
RECALL_PLAN_TYPE: type[RecallPlan] = RecallPlan
MEMORY_TYPES: tuple[LongTermMemoryType, ...] = tuple(LongTermMemoryType)
TASK_PROPOSAL_TYPE: type[TaskScopeProposal] = TaskScopeProposal
TASK_MUTATION_TYPE: type[TaskScopeMutationPlan] = TaskScopeMutationPlan
CREATE_ROUTE: TaskScopeRoute = TaskScopeRoute.CREATE_NEW
