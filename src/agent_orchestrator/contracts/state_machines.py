# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""State machines of the design's §25 (Task, Attempt, Claim) plus the Mission status
fixed by ORCH-BUILD-v1.0 §13.

Only the transitions drawn in §25 are legal; the Commit Service refuses anything
else (``IllegalTransition``).  Dynamic re-planning (step 5) does not add back-edges
either: superseded work gets a *new* Task entity.
"""

from __future__ import annotations

from enum import StrEnum


class MissionStatus(StrEnum):
    """§26.1 leaves ``status: string``; ORCH-BUILD §13 fixes this serialisable set."""

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MissionStopReason(StrEnum):
    VERIFICATION_PASSED = "verification_passed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    PLANNING_FAILED = "planning_failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """§25.1 Task 状态机."""

    BLOCKED = "BLOCKED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptStatus(StrEnum):
    """§25.2 Attempt 状态机 (PASS → COMPLETED, FAIL → RETRY_WAIT; new Attempt starts PENDING).

    ``RETRY_WAIT`` is the terminal state of a failed Attempt even when no retry
    follows (the *Task* then goes FAILED with a stop reason); §25.2 defines no
    separate Attempt FAILED state and we do not invent one.
    """

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUBMITTED = "SUBMITTED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    RETRY_WAIT = "RETRY_WAIT"
    LOST = "LOST"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class ClaimStatus(StrEnum):
    """§25.3 Claim 状态机; only VERIFIED becomes formal knowledge (§14.3)."""

    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    SUPPORTED = "SUPPORTED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"


class ResultOutcome(StrEnum):
    """§13 ``outcome``: candidate / blocked / failure / proposed_subtasks / no_progress."""

    CANDIDATE = "candidate"
    BLOCKED = "blocked"
    FAILURE = "failure"
    PROPOSED_SUBTASKS = "proposed_subtasks"
    NO_PROGRESS = "no_progress"


class IllegalTransition(ValueError):
    def __init__(self, kind: str, current: str, target: str) -> None:
        super().__init__(f"illegal {kind} transition {current} -> {target}")
        self.kind = kind
        self.current = current
        self.target = target


_MISSION: dict[MissionStatus, frozenset[MissionStatus]] = {
    MissionStatus.CREATED: frozenset({MissionStatus.PLANNING, MissionStatus.CANCELLED}),
    MissionStatus.PLANNING: frozenset(
        {MissionStatus.ACTIVE, MissionStatus.FAILED, MissionStatus.CANCELLED}
    ),
    MissionStatus.ACTIVE: frozenset(
        {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
    ),
    MissionStatus.COMPLETED: frozenset(),
    MissionStatus.FAILED: frozenset(),
    MissionStatus.CANCELLED: frozenset(),
}

# §25.1 exactly: BLOCKED→READY, READY→ACTIVE, ACTIVE→VERIFYING, VERIFYING→COMPLETED,
# VERIFYING→ACTIVE (retry / another attempt), ACTIVE→FAILED (stop condition),
# READY→CANCELLED, ACTIVE→CANCELLED.  A brand-new Task with no dependencies is
# committed directly as READY (the [*]→BLOCKED edge is the empty-dependency case).
_TASK: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY}),
    TaskStatus.READY: frozenset({TaskStatus.ACTIVE, TaskStatus.CANCELLED}),
    TaskStatus.ACTIVE: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.COMPLETED, TaskStatus.ACTIVE}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

# §25.2 plus the "other states" (LOST / TIMED_OUT / CANCELLED / SUPERSEDED), which
# may be entered from any non-terminal state.
_ATTEMPT_OTHER = frozenset(
    {
        AttemptStatus.LOST,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.CANCELLED,
        AttemptStatus.SUPERSEDED,
    }
)
_ATTEMPT: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.PENDING: frozenset({AttemptStatus.CLAIMED}) | _ATTEMPT_OTHER,
    AttemptStatus.CLAIMED: frozenset({AttemptStatus.RUNNING}) | _ATTEMPT_OTHER,
    AttemptStatus.RUNNING: frozenset({AttemptStatus.SUBMITTED, AttemptStatus.RETRY_WAIT})
    | _ATTEMPT_OTHER,
    AttemptStatus.SUBMITTED: frozenset({AttemptStatus.VERIFYING, AttemptStatus.RETRY_WAIT})
    | _ATTEMPT_OTHER,
    AttemptStatus.VERIFYING: frozenset({AttemptStatus.COMPLETED, AttemptStatus.RETRY_WAIT})
    | _ATTEMPT_OTHER,
    AttemptStatus.COMPLETED: frozenset(),
    AttemptStatus.RETRY_WAIT: frozenset(),
    AttemptStatus.LOST: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.SUPERSEDED: frozenset(),
}

_CLAIM: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.PROPOSED: frozenset({ClaimStatus.UNDER_REVIEW}),
    ClaimStatus.UNDER_REVIEW: frozenset(
        {
            ClaimStatus.VERIFIED,
            ClaimStatus.REJECTED,
            ClaimStatus.DISPUTED,
            ClaimStatus.SUPPORTED,
        }
    ),
    ClaimStatus.SUPPORTED: frozenset(
        {ClaimStatus.VERIFIED, ClaimStatus.REJECTED, ClaimStatus.DISPUTED}
    ),
    ClaimStatus.VERIFIED: frozenset({ClaimStatus.SUPERSEDED}),
    ClaimStatus.REJECTED: frozenset(),
    ClaimStatus.DISPUTED: frozenset(),
    ClaimStatus.SUPERSEDED: frozenset(),
}

TERMINAL_TASK = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
TERMINAL_ATTEMPT = frozenset(
    {
        AttemptStatus.COMPLETED,
        AttemptStatus.RETRY_WAIT,
        AttemptStatus.LOST,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.CANCELLED,
        AttemptStatus.SUPERSEDED,
    }
)
TERMINAL_MISSION = frozenset(
    {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
)


def assert_mission_transition(current: MissionStatus, target: MissionStatus) -> None:
    if target not in _MISSION[MissionStatus(current)]:
        raise IllegalTransition("mission", str(current), str(target))


def assert_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in _TASK[TaskStatus(current)]:
        raise IllegalTransition("task", str(current), str(target))


def assert_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    if target not in _ATTEMPT[AttemptStatus(current)]:
        raise IllegalTransition("attempt", str(current), str(target))


def assert_claim_transition(current: ClaimStatus, target: ClaimStatus) -> None:
    if target not in _CLAIM[ClaimStatus(current)]:
        raise IllegalTransition("claim", str(current), str(target))


__all__ = (
    "TERMINAL_ATTEMPT",
    "TERMINAL_MISSION",
    "TERMINAL_TASK",
    "AttemptStatus",
    "ClaimStatus",
    "IllegalTransition",
    "MissionStatus",
    "MissionStopReason",
    "ResultOutcome",
    "TaskStatus",
    "assert_attempt_transition",
    "assert_claim_transition",
    "assert_mission_transition",
    "assert_task_transition",
)
