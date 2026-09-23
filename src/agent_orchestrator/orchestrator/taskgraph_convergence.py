# SPDX-License-Identifier: Apache-2.0
"""Run existing cancellation/reconciliation outside TaskGraph transactions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..graph.notification_contracts import _text
from ..planning.htn.grounding import derive_id
from ..storage.store import Store, StoreConflict, StoreError
from ..storage.taskgraph_convergence import ConvergenceJob, TaskGraphConvergenceStore
from ..runtime.taskgraph_local_work import read_local_work


class ConvergenceActionKind(StrEnum):
    CANCEL_ATTEMPT = "CANCEL_ATTEMPT"
    CANCEL_SERVICE_INTENT = "CANCEL_SERVICE_INTENT"
    RECONCILE_OPERATION = "RECONCILE_OPERATION"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConvergenceAction:
    occurrence_id: str
    original_identity: str
    kind: ConvergenceActionKind

    def __post_init__(self) -> None:
        _text(self.occurrence_id, "occurrence_id")
        _text(self.original_identity, "original_identity")
        object.__setattr__(self, "kind", ConvergenceActionKind(self.kind))

    def command_key(self, job_id: str) -> str:
        return derive_id("tg-convergence-action", job_id, self.original_identity, self.kind)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConvergenceObservation:
    """Complete current read supplied by the actual H1 runtime/Operation adapter."""
    job_id: str
    job_version: int
    target_occurrences: tuple[str, ...]
    actions: tuple[ConvergenceAction, ...]
    quiescent: bool


class ConvergenceRuntime(Protocol):
    def observe(self, store: Store, job: ConvergenceJob) -> ConvergenceObservation:
        """Read every target plus shared consumers; missing/partial source raises."""
        ...

    async def cancel_attempt(self, job: ConvergenceJob, action: ConvergenceAction,
                             *, command_key: str) -> None:
        """Use original cancel/owner checks and persist its actual command result."""
        ...

    async def reconcile_operation(self, job: ConvergenceJob, action: ConvergenceAction,
                                  *, command_key: str) -> None:
        """Use original Operation identity and receipt protocol; never guess a result."""
        ...

    async def cancel_service_intent(self, job: ConvergenceJob, action: ConvergenceAction,
                                    *, command_key: str) -> None:
        """Stop the exact original Task-related service; preserve physical holds."""
        ...


class TaskGraphConvergence:
    def __init__(self, jobs: TaskGraphConvergenceStore, *, runtime: ConvergenceRuntime) -> None:
        self.jobs = jobs
        self.runtime = runtime

    def _observe(self, job: ConvergenceJob) -> ConvergenceObservation:
        observed = self.runtime.observe(self.jobs.store, job)
        expected = tuple(sorted(item.occurrence_id for item in job.targets))
        if (observed.job_id != job.job_id or observed.job_version != job.row_version
                or tuple(sorted(observed.target_occurrences)) != expected
                or type(observed.quiescent) is not bool):
            raise StoreError("TASKGRAPH_CONVERGENCE_SOURCE_INCOMPLETE")
        identities = set()
        for action in observed.actions:
            identity = (action.kind, action.original_identity)
            if action.occurrence_id not in expected or identity in identities:
                raise StoreError("TASKGRAPH_CONVERGENCE_ACTION_INVALID")
            identities.add(identity)
        if observed.quiescent and observed.actions:
            raise StoreError("TASKGRAPH_CONVERGENCE_SOURCE_CONTRADICTION")
        if observed.quiescent and read_local_work(self.jobs.store, job.mission_id).blocking_subjects(
                frozenset(target.task_id for target in job.targets)):
            raise StoreError("TASKGRAPH_CONVERGENCE_LOCAL_WORK_UNSETTLED")
        return observed

    async def advance_convergence(self, mission_id: str, job_id: str, *, now_ms: int) -> ConvergenceJob:
        if self.jobs.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_CONVERGENCE_EXTERNAL_WORK_INSIDE_TRANSACTION")
        with self.jobs.store.read_view():
            job = self.jobs.get_job(mission_id, job_id)
            if job.state in ("APPLIED", "ABANDONED"):
                return job
            observed = self._observe(job)
        if not observed.quiescent:
            for action in observed.actions:
                # Re-read the fence immediately before asking the existing authority to
                # act. That authority still checks target generation/lease and uses the
                # stable original subject; no task-wide cancellation is introduced.
                with self.jobs.store.read_view():
                    current = self.jobs.get_job(mission_id, job_id)
                    if current.row_version != job.row_version:
                        raise StoreConflict("TASKGRAPH_CONVERGENCE_CAS_CONFLICT")
                if action.kind is ConvergenceActionKind.CANCEL_ATTEMPT:
                    await self.runtime.cancel_attempt(job, action, command_key=action.command_key(job_id))
                elif action.kind is ConvergenceActionKind.CANCEL_SERVICE_INTENT:
                    await self.runtime.cancel_service_intent(job, action, command_key=action.command_key(job_id))
                else:
                    await self.runtime.reconcile_operation(job, action, command_key=action.command_key(job_id))
        with self.jobs.store.transaction():
            current = self.jobs.get_job(mission_id, job_id)
            if current.row_version != job.row_version:
                raise StoreConflict("TASKGRAPH_CONVERGENCE_CAS_CONFLICT")
            latest = self._observe(current)
            # Store independently re-reads the real quiescence/command proof before CAS.
            # READY retains its fence until actual Commit or authorized abandonment.
            return self.jobs.advance_state(mission_id, job_id, expected_version=current.row_version,
                                           ready=latest.quiescent, now_ms=now_ms)
