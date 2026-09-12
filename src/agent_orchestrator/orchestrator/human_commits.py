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

from ..artifacts.store import ArtifactStoreError, read_verified
from ..contracts import AttemptStatus, MissionStatus, MissionStopReason, TaskStatus
from ..contracts.models import sha256_hex
from ..governance.domains import DOC_DOMAIN, DomainProfileV1
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

        def domain_for(self, mission_id: str) -> DomainProfileV1: ...

        def _document_conflict_sides(self, claim_ids: Sequence[str]) -> list[dict[str, Any]]: ...

        def _validated_criterion_assessments(
            self, stored: StoredResult, task: Task, attempt: Attempt
        ) -> Any: ...

        def _assessment_binding(
            self, stored: StoredResult, task: Task, attempt: Attempt
        ) -> Any: ...

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

        stored = self._store.get_result(result_id)
        if stored is not None:
            task = self._require_task(stored.envelope.task_id)
            if self._is_document_conflict(task):
                self._cancel_document_arbitrations(
                    str(task.context.get("conflict_id")), reason=reason, result_id=result_id
                )
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
    def _is_document_conflict(self, task: Task) -> bool:
        domain = self.domain_for(task.mission_id)
        return (
            task.kind == "conflict"
            and domain.id == DOC_DOMAIN
            and domain.conflict_template.decides_with == "human_review"
        )

    def _document_arbitration_binding(self, result_id: str) -> dict[str, Any]:
        """Freeze live membership and real checked bytes, never a caller's PASS list."""
        stored = self._require_result(result_id)
        task = self._require_task(stored.envelope.task_id)
        attempt = self._require_attempt(stored.envelope.attempt_id)
        self._require_active(task.mission_id)
        conflict = self._store.get_conflict(str(task.context.get("conflict_id")))
        if (
            not self._is_document_conflict(task)
            or task.status in _ENDED_TASK
            or stored.verification_state not in {"RUNNING", "SUSPENDED"}
            or attempt.status not in OPEN_ATTEMPT_STATES
            or conflict is None
            or conflict["mission_id"] != task.mission_id
            or conflict.get("task_id") != task.id
            or conflict["state"] != "OPEN"
        ):
            raise ActionCommitError("document arbitration requires its live conflict/result")
        member_ids = list(conflict["claim_ids"])
        if (
            not member_ids
            or len(set(member_ids)) != len(member_ids)
            or {side["claim_id"] for side in conflict["sides"]} != set(member_ids)
            or any(
                (claim := self._store.get_claim(cid)) is None or claim.mission_id != task.mission_id
                for cid in member_ids
            )
        ):
            raise ActionCommitError(
                "conflict member directory is incomplete or outside this Mission"
            )
        rows = self._store.list_verifications(result_id)
        required = set(task.verification_policy) - {"human_review"}
        by_layer = {row["layer"]: row for row in rows}
        if any(row["status"] in {"FAIL", "ERROR"} for row in rows) or any(
            name not in by_layer or by_layer[name]["status"] not in {"PASS", "NEEDS_HUMAN"}
            for name in required
        ):
            raise ActionCommitError(
                "document arbitration cannot cover missing or failed verification"
            )
        self._validated_criterion_assessments(stored, task, attempt)
        binding = self._assessment_binding(stored, task, attempt)
        artifacts = []
        for aid in stored.artifacts:
            artifact = self._store.get_artifact(aid)
            if artifact is None:
                raise ActionCommitError("arbitration artifact is missing")
            try:
                read_verified(artifact)
            except ArtifactStoreError as error:
                raise ActionCommitError(
                    f"arbitration artifact unavailable: {error.reason}"
                ) from error
            artifacts.append(
                {"id": artifact.id, "path": artifact.path, "content_hash": artifact.content_hash}
            )
        return {
            "schema": 1,
            "mission_id": task.mission_id,
            "task_id": task.id,
            "result_id": result_id,
            "attempt_id": attempt.id,
            "conflict_id": conflict["conflict_id"],
            "key": conflict["key"],
            "conflict_version": conflict.get("version", 1),
            "output_hash": binding.output_hash,
            "assessment_binding_hash": binding.binding_hash,
            "artifacts": sorted(artifacts, key=lambda row: row["id"]),
            "layers": {
                name: sha256_hex(
                    {"status": by_layer[name]["status"], "detail": by_layer[name]["detail"]}
                )
                for name in sorted(required)
            },
            # _open_conflict/_dispute add revisions after assessment. Capture the
            # FINAL Store versions now, keeping assessment revisions separate.
            "sides": self._document_conflict_sides(member_ids),
        }

    def _cancel_document_arbitrations(
        self,
        conflict_id: str,
        *,
        reason: str,
        result_id: str | None = None,
        resume: bool = False,
        except_request: str | None = None,
    ) -> None:
        conflict = self._store.get_conflict(conflict_id)
        if conflict is None:
            return
        for request in self._store.list_approvals(conflict["mission_id"]):
            bound_result = request.get("binding", {}).get("result_id")
            if (
                request["kind"] != "arbitration"
                or request["state"] != "PENDING"
                or request["subject_key"] != conflict_id
                or not bound_result
                or request["request_id"] == except_request
                or (result_id is not None and bound_result != result_id)
            ):
                continue
            request.update(
                state="CANCELLED",
                version=int(request["version"]) + 1,
                closed_at=self._store.now,
                reason_closed=reason,
            )
            self._store.put_approval(request)
            self._emit(
                "ApprovalCancelled",
                conflict["mission_id"],
                key=request["request_id"],
                task_id=request.get("task_id"),
                payload={"request_id": request["request_id"], "reason": reason},
            )
            if resume:
                stored = self._store.get_result(bound_result)
                if stored is not None and stored.verification_state == "SUSPENDED":
                    self._store.set_result_verification(bound_result, state="RUNNING", verdict=None)
                    self._emit(
                        "VerificationStarted",
                        conflict["mission_id"],
                        key=f"{bound_result}:scope:{conflict.get('version', 1)}",
                        task_id=request.get("task_id"),
                        attempt_id=stored.envelope.attempt_id,
                        payload={"result_id": bound_result, "reason": reason},
                    )

    def _suspend_document_arbitration(self, result_id: str, *, owner: str | None) -> dict[str, Any]:
        stored = self._require_result(result_id)
        attempt = self._require_attempt(stored.envelope.attempt_id)
        self._require_lease(attempt, owner)
        bound = self._document_arbitration_binding(result_id)
        cid = bound["conflict_id"]
        request_id = arbitration_request_id(f"{cid}:{sha256_hex(bound)}")
        existing = self._store.get_approval(request_id)
        if existing is not None and existing["state"] == "PENDING":
            if existing.get("binding") != bound:
                raise ActionCommitError("arbitration request binding is corrupt")
            return existing
        self._cancel_review_request(result_id, reason="document_arbitration_required")
        self._cancel_document_arbitrations(
            cid, reason="arbitration_scope_replaced", except_request=request_id
        )
        options = ["keep:" + side["claim_id"] for side in bound["sides"]] + [
            "contextual",
            "unresolved",
        ]
        request = {
            "request_id": request_id,
            "kind": "arbitration",
            "topic": "conflict",
            "mission_id": bound["mission_id"],
            "task_id": bound["task_id"],
            "subject_key": cid,
            "state": "PENDING",
            "version": 1,
            "binding": bound,
            "options": options,
            "context": {
                "sides": bound["sides"],
                "result_id": result_id,
                "artifacts": bound["artifacts"],
                "key": bound["key"],
            },
            "required_count": 1,
            "grant_count": 0,
            "granted_by": [],
            "expires_at": None,
            "comments": [],
            "created_at": self._store.now,
        }
        self._store.set_result_verification(result_id, state="SUSPENDED", verdict=None)
        self._store.put_approval(request)
        self._emit(
            "VerificationSuspended",
            bound["mission_id"],
            key=request_id,
            task_id=bound["task_id"],
            attempt_id=attempt.id,
            payload={
                "result_id": result_id,
                "layer": "human_review",
                "reason": "document_arbitration",
            },
        )
        self._emit(
            "ApprovalRequested",
            bound["mission_id"],
            key=request_id,
            task_id=bound["task_id"],
            payload={
                "request_id": request_id,
                "kind": "arbitration",
                "topic": "conflict",
                "options": options,
            },
        )
        return request

    def _validate_document_arbitration(self, request: Mapping[str, Any]) -> None:
        result_id = request.get("binding", {}).get("result_id")
        if not isinstance(result_id, str):
            raise ActionCommitError("document arbitration needs an actual result binding")
        bound = self._document_arbitration_binding(result_id)
        expected_id = arbitration_request_id(f"{bound['conflict_id']}:{sha256_hex(bound)}")
        options = ["keep:" + side["claim_id"] for side in bound["sides"]] + [
            "contextual",
            "unresolved",
        ]
        if (
            request.get("binding") != bound
            or request.get("request_id") != expected_id
            or request.get("topic") != "conflict"
            or request.get("subject_key") != bound["conflict_id"]
            or request.get("mission_id") != bound["mission_id"]
            or request.get("task_id") != bound["task_id"]
            or request.get("options") != options
            or self._require_result(result_id).verification_state != "SUSPENDED"
        ):
            raise ActionCommitError("document arbitration binding or reviewed scope changed")

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
            if self._is_document_conflict(self._require_task(stored.envelope.task_id)):
                return self._suspend_document_arbitration(result_id, owner=owner)
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
            conflict = self._store.get_conflict(subject)
            task = self._store.get_task(task_id) if task_id is not None else None
            if (
                conflict is not None
                and self.domain_for(conflict["mission_id"]).id == DOC_DOMAIN
            ) or (task is not None and self._is_document_conflict(task)):
                raise ActionCommitError(
                    "document conflict arbitration requires a verified result; "
                    "use suspend_verification"
                )
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
            conflict = self._store.get_conflict(str(request["subject_key"]))
            document = (
                conflict is not None and self.domain_for(conflict["mission_id"]).id == DOC_DOMAIN
            )
            if document:
                self._validate_document_arbitration(request)
            elif ruling == "contextual":
                raise ActionCommitError(
                    "contextual rulings are only available for document conflicts"
                )
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
        contextual = ruling == "contextual"
        claim_id = None if contextual else ruling.removeprefix("keep:")
        resolution = {
            "claim_id": claim_id,
            "override_id": override_id,
            "principal_id": request.get("decided_by"),
        }
        if self.domain_for(str(request["mission_id"])).id == DOC_DOMAIN:
            resolution.update(
                ruling=ruling,
                basis=request["basis"],
                request_id=request["request_id"],
                result_id=request["binding"]["result_id"],
                claim_ids=list(conflict["claim_ids"]) if contextual else [claim_id],
                sides=request["binding"]["sides"],
                scope="this conflict only; conditions do not establish verified world knowledge",
            )
        self._store.upsert_conflict(
            {
                **conflict,
                "state": "RESOLVED_BY_HUMAN",
                "version": int(conflict.get("version", 1)) + 1,
                "resolution": resolution,
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
