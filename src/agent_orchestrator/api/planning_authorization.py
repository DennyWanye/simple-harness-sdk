# SPDX-License-Identifier: Apache-2.0
"""Authenticated system API for planning-lane grant issue/renew/revoke."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts import TERMINAL_MISSION, ContractError
from ..governance.permissions import Principal
from ..governance.planning_authorization import PlanningLanePolicy
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.store import StoreConflict


@dataclass(frozen=True, slots=True)
class PlanningGrantReceipt:
    grant_id: str
    mission_id: str
    revision: int
    active: bool
    grant_hash: str
    issuer_receipt_hash: str
    command_id: str
    expires_at_ms: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PlanningGrantReceipt:
        return cls(
            grant_id=str(row["grant_id"]),
            mission_id=str(row["mission_id"]),
            revision=int(row["revision"]),
            active=bool(row["active"]),
            grant_hash=str(row["grant_hash"]),
            issuer_receipt_hash=str(row["issuer_receipt_hash"]),
            command_id=str(row["issuer_command_id"]),
            expires_at_ms=int(row["expires_at_ms"]),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "mission_id": self.mission_id,
            "revision": self.revision,
            "active": self.active,
            "grant_hash": self.grant_hash,
            "issuer_receipt_hash": self.issuer_receipt_hash,
            "command_id": self.command_id,
            "expires_at_ms": self.expires_at_ms,
        }


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class PlanningAuthorizationApi:
    """The caller identity and tenant are fixed at construction time."""

    def __init__(
        self,
        commit: Any,
        *,
        tenant_id: str,
        principal: Principal,
        deployment: Any = None,
    ) -> None:
        if not isinstance(principal, Principal):
            raise ContractError("planning authorization needs an authenticated Principal")
        if not str(tenant_id).strip():
            raise ContractError("planning authorization needs a tenant")
        self._commit = commit
        self._store = getattr(commit, "store", commit)
        self._tenant = str(tenant_id)
        self._principal = principal
        self._deployment = deployment
        self._policy = PlanningLanePolicy()

    @property
    def policy(self) -> PlanningLanePolicy:
        return self._policy

    def _mission(self, mission_id: str) -> Any:
        mission = self._store.get_mission(str(mission_id))
        if mission is None or str(mission.tenant_id) != self._tenant:
            raise StoreConflict("planning mission is not available to this caller")
        if str(mission.status) in {str(item) for item in TERMINAL_MISSION}:
            raise StoreConflict("planning authorization cannot change a terminal mission")
        from ..governance.planning_authorization import planning_policy_for_mission
        self._policy = planning_policy_for_mission(self._store, mission_id)
        return mission

    def _require_issuer(self, row: dict[str, Any]) -> None:
        # Idempotency is not permission to read another caller's receipt. Keep
        # persisted command hashes stable and bind replay to its stored issuer.
        if row["tenant_id"] != self._tenant or row["issuer_id"] != self._principal.principal_id:
            raise StoreConflict("planning grant is not available to this caller")

    def _require_protocol(self, mission_id: str) -> None:
        row = self._store.connection.execute(
            "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None or str(row["protocol_version"]) != "planning-decision-v1":
            raise StoreConflict("planning authorization requires planning-decision-v1")

    def _command_hash(self, body: Any) -> str:
        return _digest(body)

    def issue(
        self,
        mission_id: str,
        *,
        command_id: str,
        request_id: str | None = None,
        scope_id: str = "mission",
        planner_principal_id: str | None = None,
        approval_source: str = "HUMAN",
    ) -> PlanningGrantReceipt:
        """``approval_source`` is recorded on the grant, outside the command hash:
        ``HUMAN`` (a person clicked) or ``HOST_AUTO_PERMISSION`` (the Host issued on
        the principal's behalf because their permission mode is auto).  An automatic
        grant is never written down as a person's decision."""
        if approval_source not in ("HUMAN", "HOST_AUTO_PERMISSION"):
            raise ValueError("approval_source must be HUMAN or HOST_AUTO_PERMISSION")
        mission = self._mission(mission_id)
        if request_id is not None:
            request = PlanningDecisionStore(self._store).get_planning_request(request_id)
            if request is None or request.mission_id != mission.id:
                raise StoreConflict("planning request is not available to this caller")
        self._require_protocol(mission.id)
        planner = str(planner_principal_id or self._principal.principal_id)
        body = {
            "op": "issue",
            "mission_id": mission.id,
            "tenant_id": self._tenant,
            "request_id": request_id,
            "scope_id": scope_id,
            "planner_principal_id": planner,
            "policy_version": self._policy.policy_version,
            "policy_hash": self._policy.policy_hash,
            "allowed_decisions": list(self._policy.allowed_decisions),
        }
        command_hash = self._command_hash(body)
        admission = PlanningAdmissionStore(self._store)
        replay = admission.get_grant_by_command(command_id)
        if replay is not None:
            self._require_issuer(replay)
            if replay["command_hash"] != command_hash:
                raise StoreConflict("planning authorization command id was reused")
            return PlanningGrantReceipt.from_row(replay)
        now_ms = int(self._store.now * 1000)
        grant_id = _digest(
            {
                "mission_id": mission.id,
                "planner": planner,
                "scope": scope_id,
                "command_id": command_id,
            }
        )
        if admission.get_grant(grant_id) is not None:
            raise StoreConflict("planning mission already has a different grant lineage")
        grant_hash = _digest(
            {
                **body,
                "grant_id": grant_id,
                "revision": 1,
                "not_before_ms": now_ms,
                "expires_at_ms": now_ms + self._policy.ttl_ms,
            }
        )
        receipt_hash = _digest(
            {
                "command_hash": command_hash,
                "grant_id": grant_id,
                "revision": 1,
                "grant_hash": grant_hash,
            }
        )
        row = {
            "grant_id": grant_id,
            "mission_id": mission.id,
            "tenant_id": self._tenant,
            "scope_id": scope_id,
            "planner_principal_id": planner,
            "revision": 1,
            "active": True,
            "allowed_decisions": self._policy.allowed_decisions,
            "policy_hash": self._policy.policy_hash,
            "not_before_ms": now_ms,
            "expires_at_ms": now_ms + self._policy.ttl_ms,
            "issuer_id": self._principal.principal_id,
            "issuer_command_id": command_id,
            "issuer_receipt_hash": receipt_hash,
            "grant_hash": grant_hash,
            "command_hash": command_hash,
            "reason": "issue",
            "approval_source": approval_source,
            "on_behalf_of_principal_id": self._principal.principal_id,
            "created_at": self._store.now,
        }
        binding = None
        if request_id is not None:
            binding = {
                "mission_id": mission.id,
                "tenant_id": self._tenant,
                "scope_id": scope_id,
                "planner_principal_id": planner,
                "grant_id": grant_id,
                "grant_revision": 1,
                "grant_hash": grant_hash,
                "policy_hash": self._policy.policy_hash,
                "created_at": self._store.now,
            }
        return PlanningGrantReceipt.from_row(
            admission.put_grant(row, request_id=request_id, binding=binding)
        )

    def bind_request(self, request_id: str, *, grant_id: str) -> dict[str, Any]:
        planning = PlanningDecisionStore(self._store)
        request = planning.get_planning_request(request_id)
        if request is None or str(request.mission_id) not in {
            m.id for m in self._store.list_missions() if m.tenant_id == self._tenant
        }:
            raise StoreConflict("planning request is not available to this caller")
        grant = PlanningAdmissionStore(self._store).get_grant(grant_id)
        if (
            grant is None
            or grant["mission_id"] != request.mission_id
            or grant["tenant_id"] != self._tenant
            or grant["issuer_id"] != self._principal.principal_id
        ):
            raise StoreConflict("planning grant is not available to this caller")
        return PlanningAdmissionStore(self._store).bind_request(
            request_id=request_id,
            mission_id=request.mission_id,
            tenant_id=self._tenant,
            scope_id=grant["scope_id"],
            planner_principal_id=grant["planner_principal_id"],
            grant_id=grant_id,
            grant_revision=grant["revision"],
            grant_hash=grant["grant_hash"],
            policy_hash=grant["policy_hash"],
        )

    def _change(
        self,
        grant_id: str,
        *,
        expected_revision: int,
        command_id: str,
        active: bool,
        reason: str,
        detail_reason: str = "",
    ) -> PlanningGrantReceipt:
        admission = PlanningAdmissionStore(self._store)
        body = {
            "op": reason,
            "grant_id": grant_id,
            "expected_revision": int(expected_revision),
            "active": active,
            "reason": detail_reason if detail_reason else reason,
            "mission_id": "",
        }
        current = admission.get_grant(grant_id)
        if current is not None:
            body["mission_id"] = str(current["mission_id"])
        command_hash = self._command_hash(body)
        replay = admission.get_grant_by_command(command_id)
        if replay is not None:
            self._require_issuer(replay)
            if replay.get("command_hash") != command_hash:
                raise StoreConflict("planning authorization command id was reused")
            return PlanningGrantReceipt.from_row(replay)
        if (
            current is None
            or current["tenant_id"] != self._tenant
            or current["issuer_id"] != self._principal.principal_id
        ):
            raise StoreConflict("planning grant is not available to this caller")
        if int(current["revision"]) != int(expected_revision):
            raise StoreConflict("planning grant revision is stale")
        self._mission(current["mission_id"])
        if reason == "renew" and not current["active"]:
            raise StoreConflict("a revoked planning grant cannot be renewed")
        command_hash = self._command_hash(body)
        now_ms = int(self._store.now * 1000)
        expires = (
            now_ms + self._policy.ttl_ms if reason == "renew" else int(current["expires_at_ms"])
        )
        revision = int(current["revision"]) + 1
        grant_hash = _digest(
            {
                "grant_id": grant_id,
                "revision": revision,
                "active": active,
                "policy_hash": current["policy_hash"],
                "expires_at_ms": expires,
            }
        )
        receipt_hash = _digest(
            {
                "command_hash": command_hash,
                "grant_id": grant_id,
                "revision": revision,
                "grant_hash": grant_hash,
            }
        )
        row = {
            **current,
            "revision": revision,
            "active": active,
            "not_before_ms": int(current["not_before_ms"]),
            "expires_at_ms": expires,
            "issuer_command_id": command_id,
            "issuer_receipt_hash": receipt_hash,
            "grant_hash": grant_hash,
            "command_hash": command_hash,
            "reason": detail_reason if detail_reason else reason,
            "created_at": self._store.now,
        }
        return PlanningGrantReceipt.from_row(admission.put_grant(row))

    def renew(
        self, grant_id: str, *, expected_revision: int, command_id: str
    ) -> PlanningGrantReceipt:
        return self._change(
            grant_id,
            expected_revision=expected_revision,
            command_id=command_id,
            active=True,
            reason="renew",
        )

    def revoke(
        self, grant_id: str, *, expected_revision: int, command_id: str, reason: str
    ) -> PlanningGrantReceipt:
        if not str(reason).strip():
            raise ContractError("revoking a planning grant needs a reason")
        return self._change(
            grant_id,
            expected_revision=expected_revision,
            command_id=command_id,
            active=False,
            reason="revoke",
            detail_reason=str(reason),
        )


__all__ = ("PlanningAuthorizationApi", "PlanningGrantReceipt")
