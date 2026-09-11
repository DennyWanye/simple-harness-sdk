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
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..contracts import ContractError, MissionStatus
from ..governance.permissions import (
    Principal,
    binding_of,
    decision_receipt_hash,
)
from ..governance.policies import ActionDecision, DeploymentPolicy, action_decision
from ..runtime.connectors import params_hash

if TYPE_CHECKING:
    from ..contracts import Event
    from ..storage.store import Store

OPEN_ACTION_STATES = frozenset({"PROPOSED", "AWAITING_APPROVAL", "APPROVED"})
IN_FLIGHT_ACTION_STATES = frozenset({"HANDED_OFF", "UNKNOWN"})
CLOSED_ACTION_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "REJECTED", "REVOKED", "EXPIRED", "SUPERSEDED", "CANCELLED", "REFUSED"}
)
CANDIDATE_FIELDS = ("connector", "operation", "target", "params", "reason")
ACTION_PREFIX = "action:"


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


def business_action_id(mission_id: str, connector: str, operation: str, target: str) -> str:
    """ORCH §12.3 stable business action id: the same Mission doing the same operation on
    the same target is the same real-world action across Attempts (plan D7-2)."""

    raw = "\x1f".join((mission_id, connector, operation, target))
    return "action-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ActionCommitsMixin:
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
                    state="SUPERSEDED", version=int(request["version"]) + 1, superseded_by=by
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
        request.update(state="EXPIRED", version=int(request["version"]) + 1)
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
            request.update(state="CANCELLED", version=int(request["version"]) + 1, reason=reason)
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
    "ActionCommitError",
    "ActionCommitsMixin",
    "CandidateRejected",
    "allowed_actions",
    "business_action_id",
    "check_candidate",
    "parse_action_criterion",
    "validate_candidate",
)
