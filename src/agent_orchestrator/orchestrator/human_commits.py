# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The Commit Service's human half (plan D7-8' / D7-9'): a result suspended for a
person's review, arbitration of a Verifier conflict, a takeover and a comment.  Like the
action half it is a mixin of ``CommitService``, so every row and event is written by the
single writer inside a Store transaction (ORCH §2).

Scope never widens (original §22, S7-07): a person's review answers one result, an
arbitration rules on one conflict, a takeover stops or re-runs one Task within its own
attempt limit — none of them can approve an action, add a tool or budget, or skip a
required verification layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..contracts import AttemptStatus, MissionStatus, MissionStopReason, TaskStatus
from ..governance.permissions import Principal, decision_receipt_hash
from ..observability.secrets import find_secrets
from ..scheduling.allocator import OPEN_ATTEMPT_STATES
from ..verification.human_review import arbitration_request_id, review_request_id
from .action_commits import ActionCommitError
from .state_machine import next_task

if TYPE_CHECKING:
    from ..contracts import Attempt, Event, Mission, Task
    from ..storage.store import Store, StoredResult

TAKEOVER_ACTIONS = ("stop", "retry_with_note")
_ENDED_TASK = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class HumanCommitsMixin:
    if TYPE_CHECKING:
        _store: Store

        def _emit(
            self,
            event_type: str,
            mission_id: str,
            *,
            key: str,
            task_id: str | None = None,
            attempt_id: str | None = None,
            payload: Mapping[str, Any] | None = None,
            actor_type: str = ...,
            actor_id: str = ...,
        ) -> Event: ...

        def _require_result(self, result_id: str) -> StoredResult: ...

        def _require_attempt(self, attempt_id: str) -> Attempt: ...

        def _require_task(self, task_id: str) -> Task: ...

        def _require_mission(self, mission_id: str) -> Mission: ...

        def _require_lease(self, attempt: Attempt, owner: str | None) -> None: ...

        def _close_attempt(
            self, attempt: Attempt, target: AttemptStatus, *, reason: str
        ) -> Attempt: ...

        def stop_task(
            self, task_id: str, *, stop_reason: MissionStopReason, detail: Mapping[str, Any]
        ) -> Task: ...

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _refuse_secrets(text: str) -> None:
        """A person's text becomes context for a Worker; one that looks like a secret is
        refused at the door rather than stopping the Task later (review P2-8)."""

        if find_secrets(text):
            raise ActionCommitError("the text looks like it contains a secret and is refused")

    def _require_active(self, mission_id: str) -> Mission:
        mission = self._require_mission(mission_id)
        if mission.status is not MissionStatus.ACTIVE:
            raise ActionCommitError(f"mission {mission_id} is {mission.status}; nothing to decide")
        return mission

    def _decision(
        self, request: Mapping[str, Any], principal: Principal, decision: str, nonce: str
    ) -> tuple[str, bool]:
        """The receipt of one human decision and whether it is new (a replay is not)."""

        if not isinstance(principal, Principal):
            raise ActionCommitError("a decision needs an authenticated Principal from the caller")
        if not nonce.strip():
            raise ActionCommitError("a decision needs a nonce")
        receipt = decision_receipt_hash(
            request_id=str(request["request_id"]),
            binding=request["binding"],
            principal_id=principal.principal_id,
            decision=decision,
            nonce=nonce,
        )
        known = {d["receipt_hash"] for d in self._store.list_decisions(str(request["request_id"]))}
        return receipt, receipt not in known

    def _book_decision(
        self,
        request: Mapping[str, Any],
        principal: Principal,
        decision: str,
        nonce: str,
        receipt: str,
        text: str,
    ) -> None:
        inserted = self._store.insert_decision(
            {
                "receipt_hash": receipt,
                "request_id": request["request_id"],
                "principal_id": principal.principal_id,
                "decision": decision,
                "nonce": nonce,
                "reason": text,
                "principal": principal.to_json(),
                "at": self._store.now,
            }
        )
        if not inserted:
            raise ActionCommitError(f"nonce {nonce!r} was already used on {request['request_id']}")

    def _cancel_review_request(self, result_id: str, *, reason: str) -> None:
        """Review P1-6 ③: a suspended result that is replaced or closed takes its open
        review request with it."""

        request = self._store.get_approval(review_request_id(result_id))
        if request is None or request["state"] != "PENDING":
            return
        request.update(
            state="CANCELLED",
            closed_at=self._store.now,
            version=int(request["version"]) + 1,
            reason_closed=reason,
        )
        self._store.put_approval(request)
        self._emit(
            "ApprovalCancelled",
            str(request["mission_id"]),
            key=str(request["request_id"]),
            task_id=request.get("task_id"),
            payload={"request_id": request["request_id"], "reason": reason},
        )

    # ------------------------------------------------------------ human review (D7-8')
    def suspend_verification(
        self,
        result_id: str,
        *,
        owner: str | None,
        reason: str,
        layers: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """The sixth layer reached without a person's verdict: the result waits outside the
        verification pick-up (SUSPENDED), a review request is on the books, and neither the
        Attempt nor the Task moves — nothing is accepted or failed."""

        with self._store.transaction():
            stored = self._require_result(result_id)
            request_id = review_request_id(result_id)
            existing = self._store.get_approval(request_id)
            if stored.verification_state == "SUSPENDED" and existing is not None:
                return existing
            if stored.verification_state != "RUNNING":
                raise ActionCommitError(
                    f"result {result_id} is {stored.verification_state}; cannot suspend it"
                )
            attempt = self._require_attempt(stored.envelope.attempt_id)
            self._require_lease(attempt, owner)
            task = self._require_task(stored.envelope.task_id)
            self._store.set_result_verification(result_id, state="SUSPENDED", verdict=None)
            request: dict[str, Any] = {
                "request_id": request_id,
                "kind": "review",
                "mission_id": stored.envelope.mission_id,
                "task_id": task.id,
                "subject_key": result_id,
                "state": "PENDING",
                "version": 1,
                "binding": {
                    "mission_id": stored.envelope.mission_id,
                    "task_id": task.id,
                    "result_id": result_id,
                    "attempt_id": attempt.id,
                    "artifacts": sorted(stored.artifacts),
                },
                "reason": reason,
                "required_count": 1,
                "grant_count": 0,
                "granted_by": [],
                "expires_at": None,  # a review waits; the Mission's runtime does not count it
                "summary": {
                    "task_goal": task.goal,
                    "result_summary": stored.envelope.summary,
                    "result_summary_source": "model (untrusted)",
                    "layers": [dict(layer) for layer in layers],
                },
                "comments": [],
                "created_at": self._store.now,
            }
            self._store.put_approval(request)
            self._emit(
                "VerificationSuspended",
                stored.envelope.mission_id,
                key=result_id,
                task_id=task.id,
                attempt_id=attempt.id,
                payload={"result_id": result_id, "layer": "human_review", "reason": reason},
            )
            self._emit(
                "ApprovalRequested",
                stored.envelope.mission_id,
                key=request_id,
                task_id=task.id,
                payload={
                    "request_id": request_id,
                    "kind": "review",
                    "result_id": result_id,
                    "reason": reason,
                },
            )
            return request

    def review_result(
        self,
        request_id: str,
        *,
        principal: Principal,
        verdict: str,
        note: str,
        nonce: str,
    ) -> tuple[dict[str, Any], str]:
        """A person's verdict on a suspended result.  The result goes back to the
        verification pick-up; the recorded layers that passed are reused, the sixth layer
        takes this verdict, and a FAIL's note reaches the next Attempt as feedback."""

        if verdict not in {"pass", "fail"}:
            raise ActionCommitError(f"a review verdict is pass or fail, not {verdict!r}")
        self._refuse_secrets(note)
        decision = "grant" if verdict == "pass" else "reject"
        with self._store.transaction():
            request = self._store.get_approval(request_id)
            if request is None or request["kind"] != "review":
                raise ActionCommitError(f"{request_id} is not a review request")
            receipt, fresh = self._decision(request, principal, decision, nonce)
            if not fresh:
                return request, receipt
            if request["state"] != "PENDING":
                raise ActionCommitError(f"review {request_id} is {request['state']}")
            self._require_active(str(request["mission_id"]))
            result_id = str(request["subject_key"])
            if self._require_result(result_id).verification_state != "SUSPENDED":
                raise ActionCommitError(f"result {result_id} is not waiting for a review")
            self._book_decision(request, principal, decision, nonce, receipt, note)
            request.update(
                state="GRANTED" if verdict == "pass" else "REJECTED",
                version=int(request["version"]) + 1,
                decided_by=principal.principal_id,
                note=note,
                closed_at=self._store.now,
                grant_count=1 if verdict == "pass" else 0,
                granted_by=[principal.principal_id] if verdict == "pass" else [],
            )
            self._store.put_approval(request)
            self._store.set_result_verification(result_id, state="RUNNING", verdict=None)
            self._emit(
                "ApprovalGranted" if verdict == "pass" else "ApprovalRejected",
                str(request["mission_id"]),
                key=f"{request_id}:{receipt[:16]}",
                task_id=request.get("task_id"),
                payload={
                    "request_id": request_id,
                    "kind": "review",
                    "receipt_hash": receipt,
                    "verdict": verdict,
                    "note": note,
                },
                actor_type="user",
                actor_id=principal.principal_id,
            )
            return request, receipt

    # ------------------------------------------------------------ arbitration (D7-8')
    def request_arbitration(
        self,
        mission_id: str,
        *,
        subject: str,
        topic: str,
        options: Sequence[str],
        context: Mapping[str, Any],
        task_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """A Verifier conflict goes to a person instead of ending the Mission.  Idempotent
        per subject: ``(request, created)``."""

        request_id = arbitration_request_id(subject)
        with self._store.transaction():
            existing = self._store.get_approval(request_id)
            if existing is not None:
                return existing, False
            self._require_active(mission_id)
            request: dict[str, Any] = {
                "request_id": request_id,
                "kind": "arbitration",
                "mission_id": mission_id,
                "task_id": task_id,
                "subject_key": subject,
                "topic": topic,
                "state": "PENDING",
                "version": 1,
                "binding": {"mission_id": mission_id, "subject": subject, "topic": topic},
                "options": list(options),
                "context": dict(context),
                "required_count": 1,
                "grant_count": 0,
                "granted_by": [],
                "expires_at": None,
                "comments": [],
                "created_at": self._store.now,
            }
            self._store.put_approval(request)
            self._emit(
                "ApprovalRequested",
                mission_id,
                key=request_id,
                task_id=task_id,
                payload={
                    "request_id": request_id,
                    "kind": "arbitration",
                    "topic": topic,
                    "options": list(options),
                },
            )
            return request, True

    def arbitrate(
        self,
        request_id: str,
        *,
        principal: Principal,
        ruling: str,
        basis: str,
        nonce: str,
    ) -> dict[str, Any]:
        """A person's ruling on a Verifier conflict: a HumanOverride with its basis.  A
        conflict ruling takes effect here (the chosen side, or unresolved = the Task stops);
        a judgment ruling is read by the next Mission judgment."""

        if not basis.strip():
            raise ActionCommitError("an arbitration needs a basis")
        self._refuse_secrets(basis)
        with self._store.transaction():
            request = self._store.get_approval(request_id)
            if request is None or request["kind"] != "arbitration":
                raise ActionCommitError(f"{request_id} is not an arbitration request")
            receipt, fresh = self._decision(request, principal, f"rule:{ruling}", nonce)
            if not fresh:
                return request
            if request["state"] != "PENDING":
                raise ActionCommitError(f"arbitration {request_id} is {request['state']}")
            if ruling not in request["options"]:
                raise ActionCommitError(f"ruling {ruling!r} is not one of {request['options']}")
            mission = self._require_active(str(request["mission_id"]))
            self._book_decision(request, principal, f"rule:{ruling}", nonce, receipt, basis)
            override_id = f"override:{request_id}"
            actor = {"actor_type": "user", "actor_id": principal.principal_id}
            record = {
                "override_id": override_id,
                "mission_id": mission.id,
                "principal": principal.to_json(),
                "action": "arbitrate",
                "subject": request["subject_key"],
                "topic": request["topic"],
                "ruling": ruling,
                "basis": basis,
                "receipt_hash": receipt,
                "scope": "this conflict only; no approval, tool or budget change",
                "at": self._store.now,
            }
            self._store.insert_override(record)
            self._emit(
                "HumanOverride",
                mission.id,
                key=override_id,
                task_id=request.get("task_id"),
                payload=record,
                **actor,
            )
            request.update(
                state="GRANTED",
                version=int(request["version"]) + 1,
                ruling=ruling,
                basis=basis,
                override_id=override_id,
                decided_by=principal.principal_id,
                closed_at=self._store.now,
                grant_count=1,
                granted_by=[principal.principal_id],
            )
            self._store.put_approval(request)
            self._emit(
                "ApprovalGranted",
                mission.id,
                key=f"{request_id}:{receipt[:16]}",
                task_id=request.get("task_id"),
                payload={
                    "request_id": request_id,
                    "kind": "arbitration",
                    "ruling": ruling,
                    "receipt_hash": receipt,
                },
                **actor,
            )
            if request["topic"] == "conflict":
                self._apply_conflict_ruling(request, override_id)
            return request

    def _apply_conflict_ruling(self, request: Mapping[str, Any], override_id: str) -> None:
        conflict_id = str(request["subject_key"])
        task_id = str(request.get("task_id") or "")
        ruling = str(request["ruling"])
        if ruling == "unresolved":  # the person confirms it cannot be settled: as before
            self.stop_task(
                task_id,
                stop_reason=MissionStopReason.HUMAN_OVERRIDE,
                detail={
                    "arbitration": request["request_id"],
                    "ruling": ruling,
                    "override_id": override_id,
                },
            )
            return
        conflict = self._store.get_conflict(conflict_id)
        if conflict is None:
            raise ActionCommitError(f"unknown conflict {conflict_id}")
        claim_id = ruling.removeprefix("keep:")
        self._store.upsert_conflict(
            {
                **conflict,
                "state": "RESOLVED_BY_HUMAN",
                "version": int(conflict.get("version", 1)) + 1,
                "resolution": {
                    "claim_id": claim_id,
                    "override_id": override_id,
                    "principal_id": request.get("decided_by"),
                },
            }
        )
        for attempt in self._store.list_attempts(task_id):
            if attempt.status in OPEN_ATTEMPT_STATES:
                self._close_attempt(attempt, AttemptStatus.CANCELLED, reason="resolved_by_human")
        task = self._require_task(task_id)
        if task.status is TaskStatus.VERIFYING:
            task = next_task(task, TaskStatus.ACTIVE)
            self._store.update_task(task, expected_version=task.version - 1)
        if task.status in {TaskStatus.READY, TaskStatus.ACTIVE}:
            self._store.update_task(
                next_task(task, TaskStatus.CANCELLED, failure_reason="resolved_by_human"),
                expected_version=task.version,
            )
            self._emit(
                "TaskCancelled",
                str(request["mission_id"]),
                key=task_id,
                task_id=task_id,
                payload={"reason": "resolved_by_human", "override_id": override_id},
            )
        self._emit(
            "ConflictResolvedByHuman",
            str(request["mission_id"]),
            key=conflict_id,
            task_id=task_id,
            payload={"conflict_id": conflict_id, "claim_id": claim_id, "override_id": override_id},
        )

    # ------------------------------------------------------------ takeover and comment (D7-9')
    def takeover(
        self,
        task_id: str,
        *,
        principal: Principal,
        action: str,
        basis: str,
        note: str = "",
    ) -> dict[str, Any]:
        """A person takes over a stalled or disputed Task: ``stop`` ends it (and, as every
        Task stop, the Mission); ``retry_with_note`` closes its open Attempt and the next
        one — within the Task's own attempt limit — carries the note as feedback."""

        if not isinstance(principal, Principal):
            raise ActionCommitError("a takeover needs an authenticated Principal from the caller")
        if action not in TAKEOVER_ACTIONS:
            raise ActionCommitError(f"a takeover is one of {TAKEOVER_ACTIONS}, not {action!r}")
        if not basis.strip():
            raise ActionCommitError("a takeover needs a basis")
        self._refuse_secrets(basis + "\n" + note)
        with self._store.transaction():
            task = self._require_task(task_id)
            mission = self._require_active(task.mission_id)
            if task.status in _ENDED_TASK:  # §25 has no edge back: nothing is revived
                raise ActionCommitError(f"task {task_id} is {task.status}; it cannot be taken over")
            limit = task.budget.max_attempts
            if action == "retry_with_note" and limit is not None and task.attempt_count >= limit:
                raise ActionCommitError(
                    f"task {task_id} used {task.attempt_count}/{limit} attempts;"
                    " a takeover never adds one"
                )
            ordinal = len(self._store.list_overrides(mission.id)) + 1
            override_id = f"override:takeover:{task_id}:{ordinal}"
            actor = {"actor_type": "user", "actor_id": principal.principal_id}
            record = {
                "override_id": override_id,
                "mission_id": mission.id,
                "principal": principal.to_json(),
                "action": f"takeover_{action}",
                "subject": task_id,
                "basis": basis,
                "note": note,
                "scope": "this Task only; no approval, tool, budget or verification-layer change",
                "at": self._store.now,
            }
            self._store.insert_override(record)
            self._emit(
                "HumanOverride",
                mission.id,
                key=override_id,
                task_id=task_id,
                payload=record,
                **actor,
            )
            if action == "stop":
                self.stop_task(
                    task_id,
                    stop_reason=MissionStopReason.HUMAN_OVERRIDE,
                    detail={
                        "override_id": override_id,
                        "principal_id": principal.principal_id,
                        "basis": basis,
                    },
                )
                return record
            for attempt in self._store.list_attempts(task_id):
                if attempt.status in OPEN_ATTEMPT_STATES:
                    self._close_attempt(
                        attempt, AttemptStatus.CANCELLED, reason="human_retry_with_note"
                    )
            task = self._require_task(task_id)
            if task.status is TaskStatus.VERIFYING:
                self._store.update_task(
                    next_task(task, TaskStatus.ACTIVE), expected_version=task.version
                )
            if note:
                self._comment(mission.id, task_id, principal, note, via=override_id)
            return record

    def add_comment(self, target_id: str, *, principal: Principal, text: str) -> Event:
        """``HumanCommentAdded`` (original §22): data, not an instruction.  On a Task it
        reaches that Task's next Attempt as a marked note; on a request it is kept there."""

        if not isinstance(principal, Principal):
            raise ActionCommitError("a comment needs an authenticated Principal from the caller")
        if not text.strip():
            raise ActionCommitError("an empty comment says nothing")
        self._refuse_secrets(text)
        with self._store.transaction():
            request = self._store.get_approval(target_id)
            if request is not None:
                mission_id = str(request["mission_id"])
                request["comments"] = [
                    *request.get("comments", []),
                    {"principal_id": principal.principal_id, "text": text, "at": self._store.now},
                ]
                self._store.put_approval(request)
            else:
                task = self._store.get_task(target_id)
                if task is not None:
                    mission_id = task.mission_id
                else:
                    mission_id = self._require_mission(target_id).id
            return self._comment(mission_id, target_id, principal, text)

    def _comment(
        self, mission_id: str, target_id: str, principal: Principal, text: str, *, via: str = ""
    ) -> Event:
        ordinal = sum(
            1 for e in self._store.list_events(mission_id) if e.type == "HumanCommentAdded"
        )
        return self._emit(
            "HumanCommentAdded",
            mission_id,
            key=f"{target_id}:{ordinal + 1}",
            task_id=target_id if self._store.get_task(target_id) is not None else None,
            payload={
                "target_id": target_id,
                "principal_id": principal.principal_id,
                "text": text,
                "via": via or None,
            },
            actor_type="user",
            actor_id=principal.principal_id,
        )


__all__ = ("TAKEOVER_ACTIONS", "HumanCommitsMixin")
