# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Replay (original §23.4 "用历史 Event 和 Trace 重放失败过程"; theory 12 §12; ORCH §10.2;
plan D8-1' / D8-2' / D8-3').

Replay *rebuilds facts that already happened*: a pure fold of a Mission's events into
the formal state (the field set of plan D8-2'), compared with the library's own
snapshot.  It never executes anything — no provider, connector, verifier or budget code
is imported here — and it never writes: the library is copied to a temporary directory
and the copy is opened read-only.  Running a task again under a new model or rule is an
Evaluation, not a Replay (plan §6.1).

A field the events cannot decide is ``not_covered`` — never back-filled from the
library's current value — and every gap is listed (S8-05: an incomplete trace is
reported, not completed by guesswork)."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..storage.store import Store

REPLAY_VERSION = "replay-v1"

# plan D8-2': the formal state; everything else (budgets, intents, leases, heartbeats,
# allocation scores, backpressure) is explicitly out of scope
FORMAL_FIELDS: dict[str, tuple[str, ...]] = {
    "mission": ("status", "stop_reason", "policy_version_id", "domain_id"),
    "task": ("status", "accepted_result_id"),
    "attempt": ("status",),
    "result": ("verification_state", "verdict"),
    "knowledge": ("status", "superseded_by"),
    "conflict": ("state",),
    "action": ("state", "receipt_hash"),
    "approval": ("state",),
    "override": ("present",),
}
# fields an older library does not record: expected only where the library has them
# (step 9, plan D9-3': the policy binding is formal state from schema v6 on)
# P3.3 (plan v3 D9): a library from before domain binding has the mission row but not the
# field — the same shape as the step-9 policy binding
OPTIONAL_FIELDS = frozenset({("mission", "policy_version_id"), ("mission", "domain_id")})
# events that carry no formal state (known, deliberately not projected)
NO_FORMAL_EFFECT = frozenset(
    {
        "AgentCreated",
        "AllocationDecided",
        "BudgetReleased",
        "BudgetReserved",
        "ReservationHeld",
        "HeartbeatReceived",
        "InputSubmitted",
        "IntentSettled",
        "ModelRouted",
        "RuntimeProfileUnavailable",
        "TaskGraphCommitted",
        "TaskGraphRejected",
        "TaskGraphChangeRejected",
        "TaskGraphChanged",
        "TaskDependenciesRewritten",
        "TaskPaused",
        "TaskResumed",
        "TaskRoleChanged",
        "PlanningRejected",
        "MissionCriteriaJudged",
        "VerificationLayerRecorded",
        "ClaimDisputed",
        "KnowledgeUsed",
        "SynthesisGated",
        "ManagementRequested",
        "ManagementDecided",
        "OutcomeRecorded",
        "ToolCallRejected",
        "RetrievalUnavailable",
        "BackpressureRaised",
        "BackpressureCleared",
        "ActionHandoffRefused",
        "HumanCommentAdded",
        "MissionConflict",
        "PolicyInterpreterDrift",
        "PolicyRouteUnavailable",
        "PolicySuggestionRefused",
    }
)
TERMINAL_TASK = {"COMPLETED", "FAILED", "CANCELLED"}
OPEN_RESULT = {"PENDING", "RUNNING", "SUSPENDED"}
OPEN_ACTION = {"PROPOSED", "AWAITING_APPROVAL", "APPROVED"}
OPEN_ATTEMPT = {"PENDING", "CLAIMED", "RUNNING", "SUBMITTED", "VERIFYING"}


# ------------------------------------------------------------------ sources
def library_copy(library: Path, into: Path) -> Path:
    """Copy ``orchestrator.db`` with its ``-wal`` / ``-shm`` into ``into`` (plan D8-1'):
    the library itself is never opened, so it cannot be written."""

    into.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = library.with_name(library.name + suffix)
        if source.is_file():
            shutil.copy2(source, into / (library.name + suffix))
    return into / library.name


def events_from_store(store: Store, mission_id: str) -> list[dict[str, Any]]:
    return [event.to_json() | {"seq": event.seq} for event in store.iter_events(mission_id)]


def events_from_file(path: Path) -> list[dict[str, Any]]:
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            event = json.loads(line)
            event.setdefault("seq", event.get("seq") or number)
            events.append(event)
    return events


# ------------------------------------------------------------------ the fold
class Projection:
    """The formal state an ordered, deduplicated event stream decides (plan D8-2')."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in FORMAL_FIELDS}
        self.mission_id: str | None = None
        self.applied = 0
        self.duplicates = 0
        self.unknown: Counter[str] = Counter()
        self.gaps: list[dict[str, Any]] = []
        self._result_of_attempt: dict[str, str] = {}
        self._attempt_of_result: dict[str, str] = {}
        self._action_of_request: dict[str, str] = {}
        self._kind_of_request: dict[str, str] = {}
        self._conflict_of_task: dict[str, str] = {}
        # task -> the result that passed while its TaskCompleted is not yet seen
        self._passed_waiting: dict[str, str] = {}
        self._judged = False
        self._seen: set[str] = set()

    # -- helpers
    def _obj(self, kind: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        return self.objects[kind].setdefault(str(key), {})

    def _set(self, kind: str, key: str | None, **fields: Any) -> None:
        obj = self._obj(kind, key)
        if obj is not None:
            obj.update(fields)

    def _gap(self, rule: str, **detail: Any) -> None:
        self.gaps.append({"rule": rule, **detail})

    def _need(self, kind: str, key: str | None, rule: str, event: Mapping[str, Any]) -> None:
        if key and str(key) not in self.objects[kind]:
            self._gap(rule, object=kind, id=key, at_event=event.get("id"), type=event.get("type"))

    def _close_result_of(self, attempt_id: str | None) -> None:
        result_id = self._result_of_attempt.get(str(attempt_id or ""))
        result = self.objects["result"].get(result_id or "")
        if result is not None and result.get("verification_state") in OPEN_RESULT:
            result.update(verification_state="REJECTED", verdict="superseded")

    # -- the fold
    def feed(self, events: Iterable[Mapping[str, Any]]) -> Projection:
        ordered = sorted(events, key=lambda e: (int(e.get("seq") or 0), str(e.get("id"))))
        for event in ordered:
            identity = str(event.get("id") or event.get("idempotency_key"))
            if identity in self._seen:  # the same event delivered again changes nothing
                self.duplicates += 1
                continue
            self._seen.add(identity)
            self.mission_id = self.mission_id or event.get("mission_id")
            self.apply(event)
            self.applied += 1
        return self

    def apply(self, event: Mapping[str, Any]) -> None:  # noqa: C901 - one table, read top-down
        kind = str(event.get("type"))
        p: Mapping[str, Any] = event.get("payload") or {}
        task_id = event.get("task_id")
        attempt_id = event.get("attempt_id")
        mission = str(event.get("mission_id"))
        # ---------------------------------------------------------- Mission
        if kind == "MissionCreated":
            self._set("mission", mission, status="CREATED", stop_reason=None)
            if p.get("policy_version_id"):  # step 9 (plan D9-3'): bound at creation
                self._set("mission", mission, policy_version_id=p["policy_version_id"])
            if p.get("domain_id"):  # P3.3 (plan v3 D1): frozen at creation, same shape
                self._set("mission", mission, domain_id=p["domain_id"])
        elif kind == "MissionPlanning":
            self._need("mission", mission, "mission_created_missing", event)
            self._set("mission", mission, status="PLANNING")
        elif kind == "MissionActivated":
            self._set("mission", mission, status="ACTIVE")
        elif kind == "MissionSuccessJudged":  # commits together with the terminal event
            self._judged = True
        elif kind == "MissionCompleted":
            self._set("mission", mission, status="COMPLETED", stop_reason=p.get("stop_reason"))
        elif kind == "MissionFailed":
            self._set("mission", mission, status="FAILED", stop_reason=p.get("stop_reason"))
        elif kind == "MissionCancelled":
            self._set("mission", mission, status="CANCELLED", stop_reason="cancelled")
        # ---------------------------------------------------------- Task
        elif kind == "TaskCommitted":
            status = "BLOCKED" if p.get("dependencies") else "READY"
            self._set("task", task_id, status=status, accepted_result_id=None)
        elif kind == "TaskUnblocked":
            self._need("task", task_id, "task_committed_missing", event)
            self._set("task", task_id, status="READY")
        elif kind == "TaskCompleted":
            self._need("task", task_id, "task_committed_missing", event)
            if p.get("result_id") and p.get("result_id") not in self.objects["result"]:
                self._gap("result_submitted_missing", object="result", id=p.get("result_id"))
            self._set("task", task_id, status="COMPLETED", accepted_result_id=p.get("result_id"))
            self._passed_waiting.pop(str(task_id), None)
        elif kind == "TaskFailed":
            self._set("task", task_id, status="FAILED")
            self._passed_waiting.pop(str(task_id), None)
            conflict = self._conflict_of_task.get(str(task_id))
            if conflict and self.objects["conflict"].get(conflict, {}).get("state") == "OPEN":
                self._set("conflict", conflict, state="UNRESOLVED")
        elif kind in {"TaskCancelled", "TaskSuperseded"}:
            self._set("task", task_id, status="CANCELLED")
            self._passed_waiting.pop(str(task_id), None)
        elif kind == "TaskVerificationAbandoned":
            self._set("task", task_id, status="ACTIVE")
        # ---------------------------------------------------------- Attempt / Result
        elif kind == "AttemptCreated":
            self._need("task", task_id, "task_committed_missing", event)
            self._set("attempt", attempt_id, status="PENDING")
            task = self.objects["task"].get(str(task_id or ""))
            if task is not None and task.get("status") == "READY":  # same transaction (re-review)
                task["status"] = "ACTIVE"
        elif kind == "AttemptClaimed":
            self._need("attempt", attempt_id, "attempt_created_missing", event)
            self._set("attempt", attempt_id, status="CLAIMED")
        elif kind == "AttemptStarted":
            self._need("attempt", attempt_id, "attempt_created_missing", event)
            self._set("attempt", attempt_id, status="RUNNING")
            self._set("task", task_id, status="ACTIVE")
        elif kind == "ResultSubmitted":
            self._need("attempt", attempt_id, "attempt_created_missing", event)
            result_id = str(p.get("result_id"))
            self._result_of_attempt[str(attempt_id)] = result_id
            self._attempt_of_result[result_id] = str(attempt_id)
            outcome = str(p.get("outcome") or "candidate")
            if outcome != "candidate":  # step 5: kept as history, never verified; Task stays ACTIVE
                self._set(
                    "result", result_id, verification_state="REJECTED", verdict=f"outcome:{outcome}"
                )
                self._set("attempt", attempt_id, status="RETRY_WAIT")
            else:
                self._set("result", result_id, verification_state="PENDING", verdict=None)
                self._set("attempt", attempt_id, status="SUBMITTED")
                self._set("task", task_id, status="VERIFYING")
        elif kind == "ResultRejected":  # re-review P1-A (commit_service reject_result)
            if p.get("reason") != "superseded":  # a late result is history only (D3-6')
                self._set("attempt", attempt_id, status="RETRY_WAIT")  # the Task stays ACTIVE
        elif kind == "VerificationStarted":
            self._set("result", p.get("result_id"), verification_state="RUNNING", verdict=None)
            self._set("attempt", attempt_id, status="VERIFYING")
        elif kind == "VerificationPassed":
            self._set("result", p.get("result_id"), verification_state="DONE", verdict="PASS")
            self._set("attempt", attempt_id, status="COMPLETED")
            task = self.objects["task"].get(str(task_id or ""))
            if task is not None and task.get("status") != "COMPLETED":  # TaskCompleted follows
                self._passed_waiting[str(task_id)] = str(p.get("result_id"))
        elif kind == "VerificationFailed":
            self._set("result", p.get("result_id"), verification_state="DONE", verdict="FAIL")
            self._set("attempt", attempt_id, status="RETRY_WAIT")
            task = self.objects["task"].get(str(task_id or ""))
            if task is not None and task.get("status") == "VERIFYING":
                task["status"] = "ACTIVE"
        elif kind == "VerificationSuspended":
            self._set("result", p.get("result_id"), verification_state="SUSPENDED", verdict=None)
        elif kind in {"AttemptSuperseded", "AttemptCancelled"}:
            self._set(
                "attempt",
                attempt_id,
                status="SUPERSEDED" if kind == "AttemptSuperseded" else "CANCELLED",
            )
            self._close_result_of(attempt_id)
        elif kind == "AttemptLost":
            self._set("attempt", attempt_id, status="LOST")
        elif kind == "AttemptTimedOut":
            self._set("attempt", attempt_id, status="TIMED_OUT")
        # ---------------------------------------------------------- knowledge / conflicts
        elif kind == "KnowledgeCommitted":
            self._set("knowledge", p.get("knowledge_id"), status="VERIFIED", superseded_by=None)
        elif kind == "KnowledgeSuperseded":
            self._set(
                "knowledge",
                p.get("knowledge_id") or p.get("superseded"),
                status="SUPERSEDED",
                superseded_by=p.get("superseded_by") or p.get("by"),
            )
        elif kind == "ConflictOpened":
            self._set("conflict", p.get("conflict_id"), state="OPEN")
            if p.get("task_id"):
                self._conflict_of_task[str(p["task_id"])] = str(p.get("conflict_id"))
                self._passed_waiting.pop(str(p["task_id"]), None)
                self._set("task", p.get("task_id"), status="READY", accepted_result_id=None)
        elif kind == "ConflictOpenDeferred":
            self._set("conflict", p.get("conflict_id"), state="DEFERRED")
        elif kind == "ConflictResolved":
            self._set("conflict", p.get("conflict_id"), state="RESOLVED")
        elif kind == "ConflictResolvedByHuman":
            self._set("conflict", p.get("conflict_id"), state="RESOLVED_BY_HUMAN")
        # ---------------------------------------------------------- actions / approvals
        elif kind == "ActionProposed":
            open_state = "AWAITING_APPROVAL" if str(p.get("level")) in {"L2", "L3"} else "PROPOSED"
            self._set("action", p.get("action_key"), state=open_state, receipt_hash=None)
        elif kind == "ActionRefused":
            self._set("action", p.get("action_key"), state="REFUSED", receipt_hash=None)
        elif kind in {"ActionSuperseded", "ActionCancelled"}:
            state = "SUPERSEDED" if kind == "ActionSuperseded" else "CANCELLED"
            self._set("action", p.get("action_key"), state=state)
        elif kind == "ActionHandedOff":
            self._need("action", p.get("action_key"), "action_proposed_missing", event)
            self._set("action", p.get("action_key"), state="HANDED_OFF")
        elif kind in {
            "ActionSucceeded",
            "ActionFailed",
            "ActionOutcomeUnknown",
            "ActionReconciled",
        }:
            action_state: Any = p.get("state") or {
                "ActionSucceeded": "SUCCEEDED",
                "ActionFailed": "FAILED",
                "ActionOutcomeUnknown": "UNKNOWN",
            }.get(kind)
            if action_state:
                fields: dict[str, Any] = {"state": action_state}
                if action_state == "SUCCEEDED":
                    fields["receipt_hash"] = p.get("receipt_hash")
                self._set("action", p.get("action_key"), **fields)
        elif kind == "ApprovalRequested":
            request_id = str(p.get("request_id"))
            request_kind = str(p.get("kind") or "action")
            self._kind_of_request[request_id] = request_kind
            if request_kind == "action" and p.get("action_key"):
                self._action_of_request[request_id] = str(p["action_key"])
            self._set("approval", request_id, state="PENDING")
        elif kind in {
            "ApprovalGranted",
            "ApprovalRejected",
            "ApprovalRevoked",
            "ApprovalExpired",
            "ApprovalSuperseded",
            "ApprovalCancelled",
        }:
            self._approval(kind, p)
        elif kind == "HumanOverride":
            self._set(
                "override", p.get("override_id") or event.get("idempotency_key"), present=True
            )
        elif kind not in NO_FORMAL_EFFECT:
            self.unknown[kind] += 1

    def _approval(self, kind: str, p: Mapping[str, Any]) -> None:
        request_id = str(p.get("request_id"))
        self._need("approval", request_id, "approval_requested_missing", {"type": kind})
        request_kind = self._kind_of_request.get(request_id, str(p.get("kind") or "action"))
        state = {
            "ApprovalGranted": str(p.get("state") or "GRANTED"),
            "ApprovalRejected": "REJECTED",
            "ApprovalRevoked": "REVOKED",
            "ApprovalExpired": "EXPIRED",
            "ApprovalSuperseded": "SUPERSEDED",
            "ApprovalCancelled": "CANCELLED",
        }[kind]
        self._set("approval", request_id, state=state)
        if request_kind == "review" and kind in {"ApprovalGranted", "ApprovalRejected"}:
            # review_result sends the result back to the verification pick-up
            result_id = request_id.removeprefix("review-")
            self._set("result", result_id, verification_state="RUNNING", verdict=None)
            return
        action_key = self._action_of_request.get(request_id)
        action = self.objects["action"].get(action_key or "")
        if action is None:
            return
        derived = {
            "REJECTED": "REJECTED",
            "REVOKED": "REVOKED",
            "EXPIRED": "EXPIRED",
            "SUPERSEDED": "SUPERSEDED",
            "GRANTED": "APPROVED",
        }.get(state)
        if derived and action.get("state") in OPEN_ACTION:
            action["state"] = derived

    # -- structural invariants (plan D8-2': gaps are found by structure, not by seq)
    def check_structure(self) -> None:  # noqa: C901 - one list of invariants, read top-down
        """Every outcome the record must hold; a violated invariant is a gap and the
        field it leaves open becomes undecided (``None`` → ``not_covered``), so a
        dropped outcome event can never pass for the state before it (review P1-2)."""

        mission = self.objects["mission"].get(str(self.mission_id or ""))
        status = None if mission is None else mission.get("status")
        terminal = status in {"COMPLETED", "FAILED", "CANCELLED"}
        if mission is None:
            self._gap("mission_created_missing", object="mission", id=self.mission_id)
        elif self._judged and not terminal:  # MissionSuccessJudged without its terminal event
            self._gap(
                "mission_terminal_missing", object="mission", id=self.mission_id, status=status
            )
            mission.update(status=None, stop_reason=None)
        if terminal:
            for task_id, task in self.objects["task"].items():
                if task.get("status") in {"READY", "ACTIVE", "VERIFYING"}:
                    self._gap(
                        "task_terminal_missing", object="task", id=task_id, status=task["status"]
                    )
                    task["status"] = None  # undecided: the end of this Task is not on record
            for attempt_id, attempt in self.objects["attempt"].items():
                if attempt.get("status") in OPEN_ATTEMPT:
                    self._gap(
                        "attempt_terminal_missing",
                        object="attempt",
                        id=attempt_id,
                        status=attempt["status"],
                    )
                    attempt["status"] = None
            for result_id, result in self.objects["result"].items():
                if result.get("verification_state") in OPEN_RESULT:
                    self._gap(
                        "result_terminal_missing",
                        object="result",
                        id=result_id,
                        state=result["verification_state"],
                    )
                    result.update(verification_state=None, verdict=None)
        if status == "COMPLETED":  # a completed Mission has every outcome on record
            for key, action in self.objects["action"].items():
                if action.get("state") in OPEN_ACTION | {"HANDED_OFF"}:
                    self._gap(
                        "action_outcome_missing", object="action", id=key, state=action["state"]
                    )
                    action.update(state=None, receipt_hash=None)
            for key, conflict in self.objects["conflict"].items():
                if conflict.get("state") == "OPEN":
                    self._gap("conflict_outcome_missing", object="conflict", id=key)
                    conflict["state"] = None
        for task_id, task in self.objects["task"].items():
            if task.get("status") != "COMPLETED":
                continue
            result_id = str(task.get("accepted_result_id") or "")
            if not result_id:
                self._gap("task_completed_without_result", object="task", id=task_id)
                continue
            accepted = self.objects["result"].get(result_id)
            if accepted is not None and (
                accepted.get("verification_state"),
                accepted.get("verdict"),
            ) != ("DONE", "PASS"):
                self._gap("verification_outcome_missing", object="result", id=result_id)
                accepted.update(verification_state=None, verdict=None)
                owner = self.objects["attempt"].get(self._attempt_of_result.get(result_id, ""))
                if owner is not None and owner.get("status") != "COMPLETED":
                    owner["status"] = None
        for task_id, result_id in self._passed_waiting.items():  # D8-2': passed, never completed
            self._gap("task_completed_missing", object="task", id=task_id, result_id=result_id)
            waiting = self.objects["task"].get(task_id)
            if waiting is not None:
                waiting.update(status=None, accepted_result_id=None)

    def formal(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            kind: {
                key: {name: obj.get(name) for name in FORMAL_FIELDS[kind] if name in obj}
                for key, obj in objects.items()
            }
            for kind, objects in self.objects.items()
        }


# ------------------------------------------------------------------ the library's own view
def formal_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """The same field set, read from ``Store.snapshot`` (the comparison baseline)."""

    mission = snapshot["mission"]
    return {
        "mission": {
            str(mission["id"]): {
                "status": mission.get("status"),
                "stop_reason": mission.get("stop_reason"),
                **(
                    {"policy_version_id": snapshot["mission_policy"]["version_id"]}
                    if (snapshot.get("mission_policy") or {}).get("source") not in (None, "legacy")
                    else {}
                ),
                **(
                    {"domain_id": snapshot["mission_domain"]["domain_id"]}
                    if snapshot.get("mission_domain")
                    else {}
                ),
            }
        },
        "task": {
            str(t["id"]): {
                "status": t.get("status"),
                "accepted_result_id": t.get("accepted_result_id"),
            }
            for t in snapshot.get("tasks", [])
        },
        "attempt": {
            str(a["id"]): {"status": a.get("status")} for a in snapshot.get("attempts", [])
        },
        "result": {
            str(r["envelope"]["id"]): {
                "verification_state": r.get("verification_state"),
                "verdict": r.get("verdict"),
            }
            for r in snapshot.get("results", [])
        },
        "knowledge": {
            str(k["id"]): {"status": k.get("status"), "superseded_by": k.get("superseded_by")}
            for k in snapshot.get("knowledge", [])
        },
        "conflict": {
            str(c["conflict_id"]): {"state": c.get("state")} for c in snapshot.get("conflicts", [])
        },
        "action": {
            str(a["action_key"]): {
                "state": a.get("state"),
                "receipt_hash": (a.get("receipt") or {}).get("receipt_hash"),
            }
            for a in snapshot.get("actions", [])
        },
        "approval": {
            str(r["request_id"]): {"state": r.get("state")} for r in snapshot.get("approvals", [])
        },
        "override": {
            str(o["override_id"]): {"present": True} for o in snapshot.get("human_overrides", [])
        },
    }


def compare(
    replayed: Mapping[str, Mapping[str, Mapping[str, Any]]],
    library: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Coverage = fields the events decided / fields the library holds; a mismatch is a
    decided field that differs from the library (plan D8-2')."""

    expected = decided = 0
    not_covered: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    by_kind: dict[str, dict[str, int]] = {}
    for kind, fields in FORMAL_FIELDS.items():
        kind_expected = kind_decided = 0
        for key, values in library.get(kind, {}).items():
            mine = replayed.get(kind, {}).get(key)
            for name in fields:
                if (kind, name) in OPTIONAL_FIELDS and name not in values:
                    continue  # an older library does not record it (plan D9-3')
                kind_expected += 1
                if (
                    mine is None
                    or name not in mine
                    or (mine.get(name) is None and values.get(name) is not None)
                ):
                    not_covered.append({"object": kind, "id": key, "field": name})
                    continue
                kind_decided += 1
                if mine.get(name) != values.get(name):
                    mismatches.append(
                        {
                            "object": kind,
                            "id": key,
                            "field": name,
                            "replayed": mine.get(name),
                            "library": values.get(name),
                        }
                    )
        for key in replayed.get(kind, {}):
            if key not in library.get(kind, {}):
                mismatches.append(
                    {
                        "object": kind,
                        "id": key,
                        "field": "*",
                        "replayed": "present",
                        "library": "absent",
                    }
                )
        expected += kind_expected
        decided += kind_decided
        by_kind[kind] = {"expected": kind_expected, "decided": kind_decided}
    return {
        "coverage": 1.0 if expected == 0 else round(decided / expected, 6),
        "by_kind": by_kind,
        "not_covered": not_covered,
        "mismatches": mismatches,
        "consistent": not mismatches,
    }


# ------------------------------------------------------------------ failures (D8-3')
TIMELINE_TYPES = frozenset(
    {
        "AttemptCreated",
        "AttemptStarted",
        "ResultSubmitted",
        "VerificationFailed",
        "VerificationSuspended",
        "AttemptLost",
        "AttemptTimedOut",
        "OutcomeRecorded",
        "ManagementRequested",
        "ManagementDecided",
        "TaskFailed",
        "TaskCancelled",
        "MissionFailed",
        "ActionFailed",
        "ActionOutcomeUnknown",
        "ApprovalRejected",
    }
)


def failure_timeline(
    events: Sequence[Mapping[str, Any]], projection: Projection
) -> list[dict[str, Any]]:
    """The key events of every Task that did not complete (plan D8-3')."""

    failed = {
        task_id
        for task_id, task in projection.objects["task"].items()
        if task.get("status") in {"FAILED", "CANCELLED", None}
        or (
            task.get("status") not in TERMINAL_TASK
            and projection.objects["mission"].get(str(projection.mission_id), {}).get("status")
            == "FAILED"
        )
    }
    lines = []
    for event in sorted(events, key=lambda e: int(e.get("seq") or 0)):
        kind = str(event.get("type"))
        relevant = event.get("task_id") in failed or kind in {"MissionFailed"}
        if kind == "VerificationLayerRecorded":
            payload = event.get("payload") or {}
            relevant = relevant and payload.get("status") in {"FAIL", "ERROR"}
        elif kind not in TIMELINE_TYPES:
            continue
        if not relevant:
            continue
        payload = dict(event.get("payload") or {})
        payload.pop("final_report", None)
        lines.append(
            {
                "seq": event.get("seq"),
                "type": kind,
                "task_id": event.get("task_id"),
                "attempt_id": event.get("attempt_id"),
                "detail": payload,
            }
        )
    return lines


# ------------------------------------------------------------------ entry point
def replay_mission(
    *,
    mission_id: str,
    library: Path | None = None,
    events_file: Path | None = None,
    failures: bool = False,
) -> dict[str, Any]:
    """Rebuild the formal state from events and compare it with the library's snapshot.
    ``library`` is copied first (never opened in place); ``events_file`` replays a
    redacted evidence file instead of the library's own event table."""

    if library is None and events_file is None:
        raise ValueError("replay needs a library or an events file")
    with tempfile.TemporaryDirectory(prefix="orch-replay-") as scratch:
        store: Store | None = None
        library_events: list[dict[str, Any]] = []
        snapshot: Mapping[str, Any] | None = None
        if library is not None:
            store = Store.open_readonly(library_copy(Path(library), Path(scratch)))
            try:
                if store.get_mission(mission_id) is None:
                    raise ValueError(f"mission {mission_id} is not in this library")
                library_events = events_from_store(store, mission_id)
                snapshot = store.snapshot(mission_id)
            finally:
                store.close()
        if events_file is not None:
            events = [
                e
                for e in events_from_file(Path(events_file))
                if e.get("mission_id") in (None, mission_id)
            ]
            if not events:
                raise ValueError(f"the events file holds no event of mission {mission_id}")
            source = "evidence_file (redacted: payloads may be replaced)"
        else:
            events = library_events
            source = "library (read-only copy)"
    projection = Projection().feed(events)
    projection.check_structure()
    formal = projection.formal()
    report: dict[str, Any] = {
        "version": REPLAY_VERSION,
        "mission_id": mission_id,
        "source": source,
        "events": len(events),
        "applied": projection.applied,
        "duplicates": projection.duplicates,
        "unknown_event_types": dict(projection.unknown),
        "gaps": projection.gaps,
        "formal_state": formal,
    }
    if snapshot is not None:
        report["comparison"] = compare(formal, formal_from_snapshot(snapshot))
        given = {str(e.get("id")) for e in events}
        report["missing_events"] = [
            {"id": e["id"], "type": e["type"], "seq": e.get("seq")}
            for e in library_events
            if str(e["id"]) not in given
        ]
    if failures:
        report["failure_timeline"] = failure_timeline(events, projection)
    return report


__all__ = (
    "FORMAL_FIELDS",
    "REPLAY_VERSION",
    "Projection",
    "compare",
    "events_from_file",
    "events_from_store",
    "failure_timeline",
    "formal_from_snapshot",
    "library_copy",
    "replay_mission",
)
