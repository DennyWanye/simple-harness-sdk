# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The Commit Service's step-7 half: the action ledger and the approvals (plan D7-2 /
D7-4 / D7-5 / D7-9).  It is a mixin of ``CommitService`` so the single-writer rule holds:
every row of ``actions`` / ``approvals`` / ``approval_decisions`` / ``human_overrides`` and
every related event is written here, inside a Store transaction.

Action states (this build's convention, plan §6.1; the vocabulary follows the SDK's tool
effects): PROPOSED (L0/L1, runs without approval) · AWAITING_APPROVAL · APPROVED ·
HANDED_OFF · SUCCEEDED · FAILED · UNKNOWN · REJECTED · REVOKED · EXPIRED · SUPERSEDED ·
REFUSED.  Approval request states: PENDING · GRANTED · REJECTED · REVOKED · EXPIRED ·
SUPERSEDED."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import ContractError, MissionStatus
from ..governance.budgets import BudgetExhausted
from ..governance.permissions import (
    Principal,
    binding_matches,
    binding_of,
    decision_receipt_hash,
)
from ..governance.policies import ActionDecision, DeploymentPolicy, action_decision
from ..runtime.connectors import Receipt, level_rank, params_hash

if TYPE_CHECKING:
    from ..contracts import Event
    from ..governance.budgets import BudgetLedger
    from ..storage.store import Store

OPEN_ACTION_STATES = frozenset({"PROPOSED", "AWAITING_APPROVAL", "APPROVED"})
IN_FLIGHT_ACTION_STATES = frozenset({"HANDED_OFF", "UNKNOWN"})
CLOSED_ACTION_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "REJECTED", "REVOKED", "EXPIRED", "SUPERSEDED", "CANCELLED", "REFUSED"}
)
CANDIDATE_FIELDS = ("connector", "operation", "target", "params", "reason")
ACTION_PREFIX = "action:"
HANDOFF_READY_STATES = frozenset({"APPROVED", "PROPOSED"})
MAX_HANDOFFS_PER_ACTION = 2  # one hand-off + at most one re-hand-off (SDK rehandoff_count <= 1)
OUTCOME_EVENTS = {
    "SUCCEEDED": "ActionSucceeded",
    "FAILED": "ActionFailed",
    "UNKNOWN": "ActionOutcomeUnknown",
}


class ActionCommitError(RuntimeError):
    """A decision or transition the ledger refuses (nothing was written)."""


class CandidateRejected(ContractError):
    """A candidate the policy, the Mission's scope or the schema does not allow (plan
    D7-2'' / D7-3').  Verification FAILs on it; accept re-checks and fails the result."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def validate_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The ``actions/<name>.json`` schema (plan D7-2); anything else is not a candidate."""

    if not isinstance(candidate, Mapping):
        raise ContractError("an action candidate must be a JSON object")
    missing = [name for name in CANDIDATE_FIELDS if name not in candidate]
    if missing:
        raise ContractError(f"action candidate lacks {missing}")
    extra = sorted(set(candidate) - set(CANDIDATE_FIELDS))
    if extra:  # e.g. "approved", "level", "idempotency_key" — never the model's to set
        raise ContractError(f"action candidate has unknown fields {extra}")
    for name in ("connector", "operation", "target", "reason"):
        if not isinstance(candidate[name], str) or not candidate[name].strip():
            raise ContractError(f"action candidate field {name!r} must be a non-empty string")
    if not isinstance(candidate["params"], Mapping):
        raise ContractError("action candidate field 'params' must be an object")
    return {name: candidate[name] for name in CANDIDATE_FIELDS}


def parse_action_criterion(criterion: str) -> tuple[str, str, str] | None:
    """``action:<connector>.<operation>:<target>`` (plan D7-7) — ``None`` for any other
    criterion; a malformed action criterion is a contract error."""

    if not criterion.startswith(ACTION_PREFIX):
        return None
    head, sep, target = criterion[len(ACTION_PREFIX) :].strip().partition(":")
    connector, dot, operation = head.partition(".")
    if not (sep and dot and connector.strip() and operation.strip() and target.strip()):
        raise ContractError(
            f"an action criterion is action:<connector>.<operation>:<target>, got {criterion!r}"
        )
    return connector.strip(), operation.strip(), target.strip()


def _normalize(connector: Any, target: str) -> str:
    normalize = getattr(connector, "normalize_target", None)
    return str(normalize(target)) if callable(normalize) else target.strip()


def allowed_actions(
    criteria: Sequence[str], connectors: Mapping[str, Any]
) -> set[tuple[str, str, str]]:
    """The Mission charter's action scope = its action criteria, normalised (D7-3')."""

    allowed = set()
    for criterion in criteria:
        parsed = parse_action_criterion(criterion)
        if parsed is not None:
            name, operation, target = parsed
            allowed.add((name, operation, _normalize(connectors.get(name), target)))
    return allowed


def check_candidate(
    candidate: Mapping[str, Any],
    *,
    criteria: Sequence[str],
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
) -> tuple[dict[str, Any], ActionDecision]:
    """Schema, deployment policy and Mission scope, in that order; raises
    ``CandidateRejected`` with a stable reason.  The same check runs in verification (the
    rule_check layer, forced whenever a result carries ``actions/``) and again inside the
    accept transaction on the accepted bytes."""

    try:
        cand = validate_candidate(candidate)
    except ContractError as error:
        raise CandidateRejected("invalid_candidate", str(error)) from error
    connector = connectors.get(cand["connector"])
    decision = action_decision(deployment, connector, cand["operation"])
    if decision.refused is not None:
        raise CandidateRejected(decision.refused, f"{cand['connector']}.{cand['operation']}")
    cand["target"] = _normalize(connector, cand["target"])
    triple = (cand["connector"], cand["operation"], cand["target"])
    if triple not in allowed_actions(criteria, connectors):
        raise CandidateRejected("action_out_of_scope", ".".join(triple[:2]) + ":" + triple[2])
    return cand, decision


def is_action_path(path: str) -> bool:
    """``actions/<name>.json`` in a Task's outputs is an action candidate (D7-2' / P2-1)."""

    return path.startswith("actions/") and path.endswith(".json")


def judgment_key(tasks: Sequence[Any]) -> str:
    """D7-7' ①: the integrated tree is fixed by the live Tasks' accepted results."""

    raw = "|".join(sorted(f"{task.id}={task.accepted_result_id}" for task in tasks))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def business_action_id(mission_id: str, connector: str, operation: str, target: str) -> str:
    """ORCH §12.3 stable business action id: the same Mission doing the same operation on
    the same target is the same real-world action across Attempts (plan D7-2)."""

    raw = "\x1f".join((mission_id, connector, operation, target))
    return "action-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def receipt_mismatch(action: Mapping[str, Any], receipt: Receipt | None) -> str | None:
    """A receipt counts only when it names this very action version (D7-5 ③)."""

    if receipt is None:
        return "missing"
    for name in ("idempotency_key", "params_hash", "target", "connector", "operation"):
        if getattr(receipt, name) != action.get(name):
            return name
    return None


class ActionCommitsMixin:
    if TYPE_CHECKING:
        _store: Store
        _ledger: BudgetLedger

        def _settle_subject(
            self,
            subject_id: str,
            mission_id: str,
            *,
            task_id: str | None,
            tool_calls: int | None = None,
        ) -> Mapping[str, Any]: ...

        def record_reservation_held(
            self, subject_id: str, mission_id: str, *, task_id: str | None, reason: str
        ) -> Event: ...

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

    # ------------------------------------------------------------ proposals
    def propose_action(
        self,
        candidate: Mapping[str, Any],
        *,
        mission_id: str,
        task_id: str,
        result_id: str,
        attempt_id: str,
        artifact_id: str,
        artifact_hash: str,
        connectors: Mapping[str, Any],
        deployment: DeploymentPolicy,
    ) -> dict[str, Any]:
        """Register one verified candidate (D7-2 / D7-2').  Policy and scope refusals raise
        ``CandidateRejected`` and write nothing; the ledger's own rules (in flight, already
        executed) write a REFUSED row.  The same content again returns the existing version;
        different content supersedes an *open* version only."""

        with self._store.transaction():
            mission = self._store.get_mission(mission_id)
            if mission is None or mission.status is not MissionStatus.ACTIVE:
                raise CandidateRejected("mission_not_active", mission_id)
            cand, decision = check_candidate(
                candidate,
                criteria=mission.success_criteria,
                connectors=connectors,
                deployment=deployment,
            )
            action_id = business_action_id(
                mission_id, cand["connector"], cand["operation"], cand["target"]
            )
            versions = self._store.list_action_versions(action_id)
            live = [v for v in versions if v["state"] != "REFUSED"]
            latest = live[-1] if live else None
            phash = params_hash(cand["params"])
            if (
                latest is not None
                and latest["params_hash"] == phash
                and latest["artifact_hash"] == artifact_hash
            ):
                return latest  # the same candidate delivered again
            refused: str | None = None
            after: str | None = None
            if latest is not None:
                if latest["state"] in IN_FLIGHT_ACTION_STATES:
                    refused = "action_in_flight"  # never race a handed-off version
                elif latest["state"] == "SUCCEEDED":
                    refused = "action_already_executed"  # reality moved; a new Mission asks again
                elif latest["state"] not in OPEN_ACTION_STATES:
                    after = str(latest["state"])  # a new, legitimate attempt after a failure
            version = len(versions) + 1
            record: dict[str, Any] = {
                "action_key": f"{action_id}:v{version}",
                "action_id": action_id,
                "version": version,
                "mission_id": mission_id,
                "task_id": task_id,
                "result_id": result_id,
                "attempt_id": attempt_id,
                "artifact_id": artifact_id,
                "artifact_hash": artifact_hash,
                "connector": cand["connector"],
                "operation": cand["operation"],
                "target": cand["target"],
                "params": dict(cand["params"]),
                "params_hash": phash,
                "reason": cand["reason"],
                "level": decision.level,
                "required_approvals": decision.required_approvals,
                "idempotency_key": None if refused else f"{action_id}:v{version}",
                "approval_request_id": None,
                "after": after,
                "handoffs": 0,
                "receipt": None,
                "history": [],
                "created_at": self._store.now,
            }
            if refused is not None:
                blocked_by = "" if latest is None else str(latest["action_key"])
                record.update(state="REFUSED", refused=refused, blocked_by=blocked_by)
                self._store.put_action(record)
                self._emit(
                    "ActionRefused",
                    mission_id,
                    key=record["action_key"],
                    task_id=task_id,
                    payload={
                        "action_key": record["action_key"],
                        "reason": refused,
                        "blocked_by": blocked_by,
                        **decision.to_json(),
                    },
                )
                return record
            if latest is not None and latest["state"] in OPEN_ACTION_STATES:
                self._supersede_action(latest, by=record["action_key"])
            record["state"] = "AWAITING_APPROVAL" if decision.required_approvals else "PROPOSED"
            if decision.required_approvals:
                request_id = f"approval-{record['action_key']}"
                record["approval_request_id"] = request_id
                self._store.put_approval(
                    {
                        "request_id": request_id,
                        "kind": "action",
                        "mission_id": mission_id,
                        "subject_key": record["action_key"],
                        "state": "PENDING",
                        "version": 1,
                        "binding": binding_of(record),
                        "level": decision.level,
                        # D7-4': the deployment's rules are frozen into the request
                        "required_count": decision.required_approvals,
                        "distinct_principals": bool(deployment.l3_distinct_principals),
                        "expires_at": self._store.now + float(deployment.approval_ttl_seconds),
                        "grant_count": 0,
                        "granted_by": [],
                        "summary": {
                            "connector": record["connector"],
                            "operation": record["operation"],
                            "target": record["target"],
                            "params": record["params"],
                            "reason": record["reason"],
                            "reason_source": "model (untrusted)",
                        },
                        "comments": [],
                        "created_at": self._store.now,
                    }
                )
            self._store.put_action(record)
            self._emit(
                "ActionProposed",
                mission_id,
                key=record["action_key"],
                task_id=task_id,
                payload={
                    "action_key": record["action_key"],
                    "action_id": action_id,
                    "version": version,
                    "connector": record["connector"],
                    "operation": record["operation"],
                    "target": record["target"],
                    "params_hash": phash,
                    "artifact_hash": artifact_hash,
                    "level": decision.level,
                    "after": after,
                },
            )
            if record["approval_request_id"]:
                self._emit(
                    "ApprovalRequested",
                    mission_id,
                    key=record["approval_request_id"],
                    task_id=task_id,
                    payload={
                        "request_id": record["approval_request_id"],
                        "kind": "action",
                        "action_key": record["action_key"],
                        "level": decision.level,
                        "required_count": decision.required_approvals,
                        "binding": binding_of(record),
                    },
                )
            return record

    def _supersede_action(self, action: Mapping[str, Any], *, by: str) -> None:
        old = dict(action)
        old.update(state="SUPERSEDED", superseded_by=by)
        old["history"] = [
            *old.get("history", []),
            {"state": "SUPERSEDED", "at": self._store.now, "by": by},
        ]
        self._store.put_action(old)
        request_id = old.get("approval_request_id")
        if request_id:
            request = self._store.get_approval(request_id)
            if request is not None and request["state"] in {"PENDING", "GRANTED"}:
                request.update(
                    state="SUPERSEDED",
                    closed_at=request.get("closed_at") or self._store.now,
                    version=int(request["version"]) + 1,
                    superseded_by=by,
                )
                self._store.put_approval(request)
                self._emit(
                    "ApprovalSuperseded",
                    old["mission_id"],
                    key=request_id,
                    task_id=old.get("task_id"),
                    payload={
                        "request_id": request_id,
                        "action_key": old["action_key"],
                        "superseded_by": by,
                    },
                )

    def _set_action_state(self, action_key: str, state: str, **fields: Any) -> dict[str, Any]:
        action = self._store.get_action(action_key)
        if action is None:
            raise ActionCommitError(f"unknown action {action_key}")
        action.update(state=state, **fields)
        action["history"] = [*action.get("history", []), {"state": state, "at": self._store.now}]
        self._store.put_action(action)
        return action

    # ------------------------------------------------------------ decisions
    def decide_approval(
        self,
        request_id: str,
        *,
        principal: Principal,
        decision: str,
        nonce: str,
        deployment: DeploymentPolicy,
        reason: str = "",
    ) -> tuple[dict[str, Any], str]:
        """A human decision (original §22 ApprovalGranted / ApprovalRejected).  Returns the
        request after the decision and the decision's receipt hash; replaying the same
        nonce returns the original receipt and changes nothing."""

        if not isinstance(principal, Principal):
            raise ActionCommitError("a decision needs an authenticated Principal from the caller")
        if decision not in {"grant", "reject"}:
            raise ActionCommitError(f"unknown decision {decision!r}")
        if not nonce.strip():
            raise ActionCommitError("a decision needs a nonce")
        with self._store.transaction():
            request = self._store.get_approval(request_id)
            if request is None:
                raise ActionCommitError(f"unknown approval request {request_id}")
            receipt = decision_receipt_hash(
                request_id=request_id,
                binding=request["binding"],
                principal_id=principal.principal_id,
                decision=decision,
                nonce=nonce,
            )
            known = {d["receipt_hash"] for d in self._store.list_decisions(request_id)}
            if receipt in known:
                return request, receipt  # the same receipt replayed: counted once, never twice
            mission = self._store.get_mission(str(request["mission_id"]))
            if mission is None or mission.status is not MissionStatus.ACTIVE:
                raise ActionCommitError(f"mission {request['mission_id']} is not ACTIVE")  # D7-4'
            if request["state"] == "PENDING" and self._store.now >= float(request["expires_at"]):
                self._expire_request(request)
                raise ActionCommitError(f"approval {request_id} expired")
            if request["state"] != "PENDING":
                raise ActionCommitError(f"approval {request_id} is {request['state']}")
            inserted = self._store.insert_decision(
                {
                    "receipt_hash": receipt,
                    "request_id": request_id,
                    "principal_id": principal.principal_id,
                    "decision": decision,
                    "nonce": nonce,
                    "reason": reason,
                    "principal": principal.to_json(),
                    "at": self._store.now,
                }
            )
            if not inserted:  # the same nonce with a different decision / person
                raise ActionCommitError(f"nonce {nonce!r} was already used on {request_id}")
            action_key = str(request["subject_key"])
            actor = {"actor_type": "user", "actor_id": principal.principal_id}
            if decision == "reject":
                request.update(
                    state="REJECTED",
                    closed_at=self._store.now,
                    version=int(request["version"]) + 1,
                    rejected_by=principal.principal_id,
                    reason=reason,
                )
                self._store.put_approval(request)
                if request["kind"] == "action":
                    self._set_action_state(action_key, "REJECTED")
                self._emit(
                    "ApprovalRejected",
                    request["mission_id"],
                    key=f"{request_id}:{receipt[:16]}",
                    payload={"request_id": request_id, "receipt_hash": receipt, "reason": reason},
                    **actor,
                )
                return request, receipt
            granted_by = list(request.get("granted_by") or [])
            counts = True
            if request.get("distinct_principals", True) and principal.principal_id in granted_by:
                counts = False  # one person counts once (deployment rule, plan D7-3)
            if counts:
                granted_by.append(principal.principal_id)
                request["grant_count"] = int(request.get("grant_count", 0)) + 1
            request["granted_by"] = granted_by
            request["version"] = int(request["version"]) + 1
            if int(request["grant_count"]) >= int(request["required_count"]):
                request["state"] = "GRANTED"
                request["closed_at"] = self._store.now
                request["granted_at"] = self._store.now
            self._store.put_approval(request)
            self._emit(
                "ApprovalGranted",
                request["mission_id"],
                key=f"{request_id}:{receipt[:16]}",
                payload={
                    "request_id": request_id,
                    "receipt_hash": receipt,
                    "counted": counts,
                    "grant_count": request["grant_count"],
                    "required_count": request["required_count"],
                    "state": request["state"],
                },
                **actor,
            )
            if request["state"] == "GRANTED" and request["kind"] == "action":
                self._set_action_state(action_key, "APPROVED", approved_at=self._store.now)
            return request, receipt

    def revoke_approval(
        self, request_id: str, *, principal: Principal, reason: str
    ) -> dict[str, Any]:
        """Withdraw a grant before the action was handed off (S7-04)."""

        if not isinstance(principal, Principal):
            raise ActionCommitError("a revocation needs an authenticated Principal from the caller")
        with self._store.transaction():
            request = self._store.get_approval(request_id)
            if request is None:
                raise ActionCommitError(f"unknown approval request {request_id}")
            if request["state"] not in {"PENDING", "GRANTED"}:
                raise ActionCommitError(f"approval {request_id} is {request['state']}")
            action = (
                self._store.get_action(str(request["subject_key"]))
                if request["kind"] == "action"
                else None
            )
            if action is not None and action["state"] not in OPEN_ACTION_STATES:
                raise ActionCommitError(
                    f"action {action['action_key']} is already {action['state']}"
                )
            request.update(
                state="REVOKED",
                closed_at=request.get("closed_at") or self._store.now,
                version=int(request["version"]) + 1,
                revoked_by=principal.principal_id,
                reason=reason,
            )
            self._store.put_approval(request)
            if action is not None:
                self._set_action_state(action["action_key"], "REVOKED")
            self._emit(
                "ApprovalRevoked",
                request["mission_id"],
                key=request_id,
                payload={"request_id": request_id, "reason": reason},
                actor_type="user",
                actor_id=principal.principal_id,
            )
            return request

    def _expire_request(self, request: dict[str, Any]) -> None:
        request.update(
            state="EXPIRED",
            closed_at=request.get("closed_at") or self._store.now,
            version=int(request["version"]) + 1,
        )
        self._store.put_approval(request)
        if request["kind"] == "action":
            action = self._store.get_action(str(request["subject_key"]))
            if action is not None and action["state"] in OPEN_ACTION_STATES:
                self._set_action_state(action["action_key"], "EXPIRED")
        self._emit(
            "ApprovalExpired",
            request["mission_id"],
            key=str(request["request_id"]),
            payload={"request_id": request["request_id"], "expires_at": request["expires_at"]},
        )

    def expire_approvals(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        """Requests past their validity that were not handed off yet expire (S7-04)."""

        expired = []
        with self._store.transaction():
            for request in self._store.list_approvals(mission_id, "PENDING", "GRANTED"):
                if self._store.now < float(request["expires_at"]):
                    continue
                if request["kind"] == "action":
                    action = self._store.get_action(str(request["subject_key"]))
                    if action is not None and action["state"] not in OPEN_ACTION_STATES:
                        continue  # already handed off: the grant was used in time
                self._expire_request(request)
                expired.append(request)
        return expired

    # ------------------------------------------------------------ hand-off (D7-5')
    def begin_handoff(
        self,
        action_key: str,
        *,
        owner: str,
        lease_seconds: float,
        connectors: Mapping[str, Any],
        deployment: DeploymentPolicy,
        rehandoff: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The outbox step of a real action, in one transaction: re-check everything the
        approval was bound to, reserve the budget, then write HANDED_OFF with the owner, a
        lease and the decision receipts that authorised it.  ``(action, None)`` = the caller
        may now call the connector; ``(None, reason)`` = it may not."""

        from .commit_service import mission_account  # noqa: PLC0415 - import cycle

        with self._store.transaction():
            action = self._store.get_action(action_key)
            if action is None:
                raise ActionCommitError(f"unknown action {action_key}")
            reason = self._handoff_refusal(
                action, connectors=connectors, deployment=deployment, rehandoff=rehandoff
            )
            subject = f"action:{action_key}"
            if reason is None:
                spec = connectors[str(action["connector"])].operations[str(action["operation"])]
                try:  # re-hand-off finds the held reservation (idempotent per subject)
                    self._ledger.reserve(
                        account_id=mission_account(str(action["mission_id"])),
                        subject_id=subject,
                        tokens=0,
                        cost_micros=int(spec.cost_micros_ceiling or 0),
                        counts_attempt=False,
                        tool_calls=1,
                        mission_id=str(action["mission_id"]),
                    )
                except BudgetExhausted:
                    reason = "budget_exhausted"
            if reason is not None:
                self._emit(
                    "ActionHandoffRefused",
                    str(action["mission_id"]),
                    key=f"{action_key}:{reason}",
                    task_id=action.get("task_id"),
                    payload={"action_key": action_key, "reason": reason, "rehandoff": rehandoff},
                )
                if rehandoff and action.get("reconcile") == "CONFIRMED_NOT_STARTED":
                    # an authoritative "never happened" that may not run again ends as FAILED
                    self._resolve_action(action, "FAILED", error=f"not_started:{reason}")
                elif reason == "approval_expired":
                    request = self._store.get_approval(str(action["approval_request_id"]))
                    if request is not None and request["state"] in {"PENDING", "GRANTED"}:
                        self._expire_request(request)
                return None, reason
            request_id = action.get("approval_request_id")
            receipts = (
                []
                if not request_id
                else [
                    d["receipt_hash"]
                    for d in self._store.list_decisions(str(request_id))
                    if d["decision"] == "grant"
                ]
            )
            handoffs = int(action.get("handoffs") or 0) + 1
            updated = self._set_action_state(
                action_key,
                "HANDED_OFF",
                owner=owner,
                lease_expires_at=self._store.now + float(lease_seconds),
                handoffs=handoffs,
                handed_off_at=self._store.now,
                decision_receipts=receipts,
                reservation_subject=subject,
                reconcile=None,
            )
            self._emit(
                "ActionHandedOff",
                str(action["mission_id"]),
                key=f"{action_key}:h{handoffs}",
                task_id=action.get("task_id"),
                payload={
                    "action_key": action_key,
                    "idempotency_key": action["idempotency_key"],
                    "handoff": handoffs,
                    "owner": owner,
                    "decision_receipts": receipts,
                    "rehandoff": rehandoff,
                },
            )
            return updated, None

    def _handoff_refusal(
        self,
        action: Mapping[str, Any],
        *,
        connectors: Mapping[str, Any],
        deployment: DeploymentPolicy,
        rehandoff: bool,
    ) -> str | None:
        mission = self._store.get_mission(str(action["mission_id"]))
        if mission is None or mission.status is not MissionStatus.ACTIVE:
            return "mission_not_active"
        if rehandoff:
            if action["state"] != "UNKNOWN" or action.get("reconcile") != "CONFIRMED_NOT_STARTED":
                return "rehandoff_needs_confirmed_not_started"
            if int(action.get("handoffs") or 0) >= MAX_HANDOFFS_PER_ACTION:
                return "rehandoff_exhausted"
        elif action["state"] not in HANDOFF_READY_STATES:
            return f"not_ready:{action['state']}"
        live = [
            v
            for v in self._store.list_action_versions(str(action["action_id"]))
            if v["state"] != "REFUSED"
        ]
        if not live or live[-1]["action_key"] != action["action_key"]:
            return "not_current_version"
        decision = action_decision(
            deployment, connectors.get(str(action["connector"])), str(action["operation"])
        )
        if decision.refused is not None:
            return decision.refused
        if level_rank(decision.level) > level_rank(str(action["level"])):
            return "level_raised"  # the approval was given under a milder level
        if int(action.get("required_approvals") or 0):
            request = self._store.get_approval(str(action.get("approval_request_id") or ""))
            if request is None or request["state"] != "GRANTED":
                return "approval_not_granted"
            if self._store.now >= float(request["expires_at"]):
                return "approval_expired"
            if not binding_matches(request, action):
                return "binding_mismatch"
        if not rehandoff:
            started = sum(
                1
                for a in self._store.list_actions(str(action["mission_id"]))
                if int(a.get("handoffs") or 0) > 0
            )
            if started >= int(deployment.max_action_handoffs_per_mission):
                return "handoff_cap_reached"
        return None

    def _update_action(self, action_key: str, **fields: Any) -> dict[str, Any]:
        action = self._store.get_action(action_key)
        if action is None:
            raise ActionCommitError(f"unknown action {action_key}")
        action.update(fields)
        self._store.put_action(action)
        return action

    def _resolve_action(
        self,
        action: Mapping[str, Any],
        state: str,
        *,
        receipt: Receipt | None = None,
        error: str = "",
        event: str | None = None,
        extra: Mapping[str, Any] | None = None,
        actor: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """HANDED_OFF / UNKNOWN → SUCCEEDED / FAILED settle the reservation with the calls
        actually made; → UNKNOWN holds it (ORCH §12.2)."""

        key = str(action["action_key"])
        mission_id = str(action["mission_id"])
        subject = str(action.get("reservation_subject") or f"action:{key}")
        fields: dict[str, Any] = {"error": error or None}
        if receipt is not None:
            fields["receipt"] = receipt.to_json()
        updated = self._set_action_state(key, state, **fields)
        if state in {"SUCCEEDED", "FAILED"}:
            if self._ledger.reservation(subject) is not None:
                self._settle_subject(
                    subject, mission_id, task_id=None, tool_calls=int(action.get("handoffs") or 0)
                )
        else:
            self.record_reservation_held(
                subject, mission_id, task_id=None, reason="action_outcome_unknown"
            )
        self._emit(
            event or OUTCOME_EVENTS[state],
            mission_id,
            key=f"{key}:h{int(action.get('handoffs') or 0)}:{state}",
            task_id=action.get("task_id"),
            payload={
                "action_key": key,
                "state": state,
                "error": error or None,
                "receipt_hash": None if receipt is None else receipt.receipt_hash,
                **dict(extra or {}),
            },
            **dict(actor or {}),
        )
        return updated

    def record_action_outcome(
        self,
        action_key: str,
        *,
        owner: str,
        outcome: str,
        receipt: Receipt | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """What the connector call returned (D7-5 ③④).  Only the owner of the live hand-off
        may record it; a late answer after someone else resolved the action is ignored."""

        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ActionCommitError(f"unknown outcome {outcome!r}")
        with self._store.transaction():
            action = self._store.get_action(action_key)
            if action is None:
                raise ActionCommitError(f"unknown action {action_key}")
            if action["state"] != "HANDED_OFF" or action.get("owner") != owner:
                return action
            if outcome == "succeeded":
                mismatch = receipt_mismatch(action, receipt)
                if mismatch is not None:  # a receipt for something else proves nothing
                    outcome, error, receipt = "unknown", f"receipt_mismatch:{mismatch}", None
            state = {"succeeded": "SUCCEEDED", "failed": "FAILED", "unknown": "UNKNOWN"}[outcome]
            return self._resolve_action(action, state, receipt=receipt, error=error)

    def record_reconciliation(
        self, action_key: str, *, verdict: str, receipt: Receipt | None = None
    ) -> dict[str, Any]:
        """The answer of an authoritative lookup by idempotency key (D7-5 ⑤): COMPLETED →
        SUCCEEDED; CONFIRMED_NOT_STARTED → may be handed off again with the *same* key;
        STILL_UNKNOWN → stays UNKNOWN and waits for a person.  A HANDED_OFF action whose
        lease lapsed without an outcome (a crash) is UNKNOWN first."""

        if verdict not in {"COMPLETED", "CONFIRMED_NOT_STARTED", "STILL_UNKNOWN"}:
            raise ActionCommitError(f"unknown reconciliation verdict {verdict!r}")
        with self._store.transaction():
            action = self._store.get_action(action_key)
            if action is None:
                raise ActionCommitError(f"unknown action {action_key}")
            lapsed = (
                action["state"] == "HANDED_OFF"
                and float(action.get("lease_expires_at") or 0.0) <= self._store.now
            )
            if action["state"] != "UNKNOWN" and not lapsed:
                return action
            if lapsed:
                action = self._resolve_action(
                    action, "UNKNOWN", error="lease_lapsed_without_outcome"
                )
            handoffs = int(action.get("handoffs") or 0)
            note = None
            if verdict == "COMPLETED":
                mismatch = receipt_mismatch(action, receipt)
                if mismatch is None:
                    return self._resolve_action(
                        action,
                        "SUCCEEDED",
                        receipt=receipt,
                        event="ActionReconciled",
                        extra={"verdict": verdict},
                    )
                verdict, note = "STILL_UNKNOWN", f"receipt_mismatch:{mismatch}"
            if verdict == "CONFIRMED_NOT_STARTED":
                updated = self._update_action(action_key, reconcile=verdict)
            else:
                updated = self._update_action(
                    action_key, reconcile=verdict, needs_human=True, reconcile_note=note
                )
            self._emit(
                "ActionReconciled",
                str(action["mission_id"]),
                key=f"{action_key}:h{handoffs}:{verdict}",
                task_id=action.get("task_id"),
                payload={"action_key": action_key, "verdict": verdict, "note": note},
            )
            return updated

    def override_action_outcome(
        self,
        action_key: str,
        *,
        principal: Principal,
        outcome: str,
        basis: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """A person rules on an UNKNOWN action the lookup cannot settle (D7-5' human exit):
        a HumanOverride with basis and evidence; the ruling covers this version only."""

        if not isinstance(principal, Principal):
            raise ActionCommitError("a ruling needs an authenticated Principal from the caller")
        if outcome not in {"succeeded", "failed"}:
            raise ActionCommitError(f"a ruling is succeeded or failed, not {outcome!r}")
        if not basis.strip() or not evidence:
            raise ActionCommitError("a ruling on an UNKNOWN action needs a basis and evidence")
        with self._store.transaction():
            action = self._store.get_action(action_key)
            if action is None or action["state"] != "UNKNOWN":
                raise ActionCommitError(f"only an UNKNOWN action takes a ruling ({action_key})")
            override_id = f"override:{action_key}:h{int(action.get('handoffs') or 0)}"
            actor = {"actor_type": "user", "actor_id": principal.principal_id}
            record = {
                "override_id": override_id,
                "mission_id": action["mission_id"],
                "principal": principal.to_json(),
                "action": "resolve_unknown_action",
                "subject": action_key,
                "outcome": outcome,
                "basis": basis,
                "evidence": dict(evidence),
                "scope": "this action version only",
                "at": self._store.now,
            }
            self._store.insert_override(record)
            self._emit(
                "HumanOverride",
                str(action["mission_id"]),
                key=override_id,
                task_id=action.get("task_id"),
                payload=record,
                **actor,
            )
            return self._resolve_action(
                action,
                "SUCCEEDED" if outcome == "succeeded" else "FAILED",
                error="" if outcome == "succeeded" else "human_ruled_failed",
                extra={"resolved_by": principal.principal_id, "override_id": override_id},
                actor=actor,
            )

    # ------------------------------------------------------------ closure (D7-2'' / D7-7')
    def _action_candidates(
        self,
        stored: Any,
        task: Any,
        mission: Any,
        *,
        connectors: Mapping[str, Any] | None,
        deployment: DeploymentPolicy | None,
    ) -> tuple[list[tuple[Any, dict[str, Any]]], dict[str, Any] | None]:
        """Re-read every accepted ``actions/*.json`` from its stored bytes, inside the accept
        transaction (review P1-1: TOCTOU); a rejection is a rule_check failure, never a crash."""

        found: list[tuple[Any, dict[str, Any]]] = []
        for artifact_id in stored.artifacts:
            artifact = self._store.get_artifact(artifact_id)
            if artifact is None or not is_action_path(artifact.path):
                continue
            try:
                if artifact.path not in task.outputs:
                    raise CandidateRejected("undeclared_action_output", artifact.path)
                raw = Path(artifact.storage_uri).read_bytes() if artifact.storage_uri else b""
                if hashlib.sha256(raw).hexdigest() != artifact.content_hash:
                    raise CandidateRejected("artifact_bytes_mismatch", artifact.path)
                candidate = json.loads(raw.decode("utf-8"))
                check_candidate(
                    candidate,
                    criteria=mission.success_criteria,
                    connectors=connectors or {},
                    deployment=deployment or DeploymentPolicy(),
                )
            except (CandidateRejected, ValueError, OSError) as error:
                reason = getattr(error, "reason", "invalid_candidate")
                return [], {
                    "layer": "rule_check",
                    "status": "FAIL",
                    "summary": f"action_candidate_rejected ({reason}): {error}",
                    "detail": {"reason": reason, "path": artifact.path},
                }
            found.append((artifact, candidate))
        return found, None

    def record_criteria_judgment(
        self,
        mission_id: str,
        key: str,
        judgments: Sequence[Mapping[str, Any]],
        *,
        summary: str,
    ) -> None:
        """D7-7' ①: the non-action criteria are judged once per integrated tree and the
        judgment is on the books — waiting for a person never re-runs pytest or a Critic."""

        with self._store.transaction():
            self._emit(
                "MissionCriteriaJudged",
                mission_id,
                key=f"{mission_id}:{key}",
                payload={
                    "tree_key": key,
                    "judgments": [dict(item) for item in judgments],
                    "summary": summary,
                },
            )

    def criteria_judgment(
        self, mission_id: str, key: str
    ) -> tuple[list[dict[str, Any]], str] | None:
        for event in reversed(self._store.list_events(mission_id)):
            if event.type == "MissionCriteriaJudged" and event.payload.get("tree_key") == key:
                return (
                    [dict(item) for item in event.payload.get("judgments", [])],
                    str(event.payload.get("summary", "")),
                )
        return None

    def action_for_criterion(
        self, mission_id: str, criterion: str, connectors: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The latest non-REFUSED version of the business action an ``action:`` criterion names."""

        parsed = parse_action_criterion(criterion)
        if parsed is None:
            return None
        name, operation, target = parsed
        action_id = business_action_id(
            mission_id, name, operation, _normalize(connectors.get(name), target)
        )
        live = [v for v in self._store.list_action_versions(action_id) if v["state"] != "REFUSED"]
        return live[-1] if live else None

    def cancel_open_actions(self, mission_id: str, *, reason: str) -> list[dict[str, Any]]:
        """D7-4' / D7-5': a Mission that ends (cancelled, failed) or work that is replaced
        closes its *open* actions and requests as CANCELLED.  Handed-off and UNKNOWN actions
        are never touched — reality may already have moved; reconciliation continues."""

        with self._store.transaction():
            return self._cancel_open_actions(mission_id, reason=reason)

    def _cancel_open_actions(self, mission_id: str, *, reason: str) -> list[dict[str, Any]]:
        cancelled = []
        for action in self._store.list_actions(mission_id, *sorted(OPEN_ACTION_STATES)):
            cancelled.append(
                self._set_action_state(action["action_key"], "CANCELLED", reason=reason)
            )
        for request in self._store.list_approvals(mission_id, "PENDING", "GRANTED"):
            if request["kind"] == "action":
                subject = self._store.get_action(str(request["subject_key"]))
                if subject is not None and subject["state"] != "CANCELLED":
                    continue  # handed off: the grant stays as the audit of what ran
            request.update(
                state="CANCELLED",
                closed_at=request.get("closed_at") or self._store.now,
                version=int(request["version"]) + 1,
                reason=reason,
            )
            self._store.put_approval(request)
            self._emit(
                "ApprovalCancelled",
                mission_id,
                key=str(request["request_id"]),
                payload={"request_id": request["request_id"], "reason": reason},
            )
        return cancelled


__all__ = (
    "CLOSED_ACTION_STATES",
    "IN_FLIGHT_ACTION_STATES",
    "OPEN_ACTION_STATES",
    "ACTION_PREFIX",
    "HANDOFF_READY_STATES",
    "MAX_HANDOFFS_PER_ACTION",
    "ActionCommitError",
    "ActionCommitsMixin",
    "CandidateRejected",
    "allowed_actions",
    "business_action_id",
    "check_candidate",
    "is_action_path",
    "judgment_key",
    "parse_action_criterion",
    "receipt_mismatch",
    "validate_candidate",
)
