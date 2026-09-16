# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Serialisation contracts (§26) and state machines (§25).

The FULL-TARGET-1.4 semantic contracts (P1.1) live in the ``evidence_state``,
``htn``, ``obligations`` and ``resolution`` submodules; they are re-exported as
modules rather than name by name, because they carry well over a hundred types
and importing them wholesale here would say nothing about which belong together.
"""

from . import evidence_state, htn, ids, obligations, resolution, semantic_base
from .assessments import ASSESSMENT_SCHEMA_VERSION, CriterionAssessmentV1
from .evidence_state import (
    PreconditionPhase,
    SupportCount,
    TemporalUse,
    TruthValue,
    Validity,
    ValidityWitness,
)
from .fragments import (
    FragmentProposalV1,
    FragmentValidationDecisionV1,
    ScopeProjectionV1,
    TaskRevisionV1,
)
from .htn import (
    MethodContract,
    MethodInstanceDraft,
    PlanProposal,
    ProposedPlanDelta,
    TaskForm,
    TaskSemanticBindingV1,
)
from .models import (
    CONTRACT_SCHEMA_VERSION,
    STEP2_IMPLEMENTED_LAYERS,
    VERIFICATION_LAYERS,
    Artifact,
    Attempt,
    Budget,
    Claim,
    ClaimProposal,
    ContractError,
    Event,
    LimitationV1,
    Mission,
    ResultEnvelope,
    SourceCitation,
    Task,
    jsonable,
    sha256_hex,
)
from .obligations import Obligation, ObligationLedger
from .resolution import (
    Criterion,
    GoalResolution,
    RequirementsRevision,
    ReviewPurpose,
    ReviewRecord,
)
from .semantic_base import EvidenceRef, TypedRef, VersionedRef
from .state_machines import (
    TERMINAL_ATTEMPT,
    TERMINAL_MISSION,
    TERMINAL_TASK,
    AttemptStatus,
    ClaimStatus,
    IllegalTransition,
    MissionStatus,
    MissionStopReason,
    ResultOutcome,
    TaskStatus,
    assert_attempt_transition,
    assert_claim_transition,
    assert_mission_transition,
    assert_task_transition,
)

__all__ = (
    "ASSESSMENT_SCHEMA_VERSION",
    "CriterionAssessmentV1",
    "CONTRACT_SCHEMA_VERSION",
    "STEP2_IMPLEMENTED_LAYERS",
    "TERMINAL_ATTEMPT",
    "TERMINAL_MISSION",
    "TERMINAL_TASK",
    "VERIFICATION_LAYERS",
    "Artifact",
    "Attempt",
    "AttemptStatus",
    "Budget",
    "Claim",
    "ClaimProposal",
    "ClaimStatus",
    "ContractError",
    "Criterion",
    "EvidenceRef",
    "Event",
    "FragmentProposalV1",
    "FragmentValidationDecisionV1",
    "LimitationV1",
    "IllegalTransition",
    "GoalResolution",
    "MethodContract",
    "MethodInstanceDraft",
    "Mission",
    "MissionStatus",
    "MissionStopReason",
    "Obligation",
    "ObligationLedger",
    "PlanProposal",
    "PreconditionPhase",
    "ProposedPlanDelta",
    "RequirementsRevision",
    "ResultEnvelope",
    "ResultOutcome",
    "ReviewPurpose",
    "ReviewRecord",
    "SourceCitation",
    "ScopeProjectionV1",
    "SupportCount",
    "Task",
    "TaskForm",
    "TaskSemanticBindingV1",
    "TaskStatus",
    "TaskRevisionV1",
    "TemporalUse",
    "TruthValue",
    "TypedRef",
    "Validity",
    "ValidityWitness",
    "VersionedRef",
    "assert_attempt_transition",
    "assert_claim_transition",
    "assert_mission_transition",
    "assert_task_transition",
    "evidence_state",
    "htn",
    "ids",
    "jsonable",
    "obligations",
    "resolution",
    "semantic_base",
    "sha256_hex",
)
