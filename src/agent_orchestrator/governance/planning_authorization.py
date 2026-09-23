# SPDX-License-Identifier: Apache-2.0
"""Planning-lane authority: policy, durable grant snapshots, and pure checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from simple_harness.contracts import canonical_json

from ..contracts import ContractError
from ..orchestrator.plan_commits import PlanPrincipal
from ..storage.store import StoreError

PLANNING_POLICY_VERSION = "planning-lane-policy-v1"
PLANNING_DECISIONS = (
    "REFINE", "REPAIR/REPLACE_METHOD", "WAIT", "NO_CHANGE", "DECLARE_BLOCKED",
)


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanningLanePolicy:
    policy_version: str = PLANNING_POLICY_VERSION
    allowed_decisions: tuple[str, ...] = PLANNING_DECISIONS
    ttl_ms: int = 24 * 60 * 60 * 1000

    def __post_init__(self) -> None:
        if self.policy_version not in {PLANNING_POLICY_VERSION, "planning-lane-policy-v2", "planning-lane-policy-v3"}:
            raise ContractError("unknown planning lane policy")
        if not self.allowed_decisions or len(set(self.allowed_decisions)) != len(
            self.allowed_decisions
        ):
            raise ContractError("planning policy needs unique allowed decisions")
        if any(not isinstance(value, str) or not value.strip() for value in self.allowed_decisions):
            raise ContractError("planning policy decisions must be named strings")
        if isinstance(self.ttl_ms, bool) or self.ttl_ms <= 0:
            raise ContractError("planning policy ttl must be positive")

    @property
    def policy_hash(self) -> str:
        return _hash({
            "policy_version": self.policy_version,
            "allowed_decisions": list(self.allowed_decisions),
            "ttl_ms": self.ttl_ms,
        })


def planning_policy_for_mission(store: Any, mission_id: str) -> PlanningLanePolicy:
    from ..storage.planning_decision_store import PlanningDecisionStore
    binding = PlanningDecisionStore(store).get_mission_protocol(mission_id)
    if binding is not None and int(binding["package_version"]) >= 7:
        from ..contracts.planning_decisions import H4_DECISION_ENABLEMENT
        return PlanningLanePolicy(policy_version="planning-lane-policy-v3",
            allowed_decisions=tuple(key for key, value in H4_DECISION_ENABLEMENT.items() if value.executable))
    if binding is not None and int(binding["package_version"]) >= 6:
        return PlanningLanePolicy(policy_version="planning-lane-policy-v2",
                                  allowed_decisions=(*PLANNING_DECISIONS, "REQUEST_EVIDENCE", "REQUEST_HUMAN", "PROPOSE_METHOD"))
    return PlanningLanePolicy()


@dataclass(frozen=True, slots=True)
class SourceUnavailable:
    source: str
    detail: str
    coverage: str = "INCOMPLETE"
    reason_code: str = "SOURCE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class PlanningAuthorizationSnapshot:
    request_id: str
    mission_id: str
    tenant_id: str
    scope_id: str
    planner_principal_id: str
    grant_id: str
    grant_revision: int
    grant_hash: str
    issuer_id: str
    issuer_command_id: str
    issuer_receipt_hash: str
    allowed_decisions: tuple[str, ...]
    policy_hash: str
    not_before_ms: int
    expires_at_ms: int
    active: bool
    checked_at_ms: int

    @property
    def expired(self) -> bool:
        return self.checked_at_ms >= self.expires_at_ms

    def to_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "mission_id": self.mission_id,
            "tenant_id": self.tenant_id, "scope_id": self.scope_id,
            "planner_principal_id": self.planner_principal_id, "grant_id": self.grant_id,
            "grant_revision": self.grant_revision, "grant_hash": self.grant_hash,
            "issuer_id": self.issuer_id, "issuer_command_id": self.issuer_command_id,
            "issuer_receipt_hash": self.issuer_receipt_hash,
            "allowed_decisions": list(self.allowed_decisions), "policy_hash": self.policy_hash,
            "not_before_ms": self.not_before_ms, "expires_at_ms": self.expires_at_ms,
            "active": self.active, "checked_at_ms": self.checked_at_ms,
        }


class PlanningAuthorityReader(Protocol):
    def get_planning_request(self, request_id: str) -> Any: ...
    def get_mission(self, mission_id: str) -> Any: ...
    def get_request_binding(self, request_id: str) -> Mapping[str, Any] | None: ...
    def get_grant(self, grant_id: str, revision: int | None = None) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class StorePlanningAuthorityReader:
    planning: Any
    store: Any

    def get_planning_request(self, request_id: str) -> Any:
        if hasattr(self.planning, "get_planning_request"):
            return self.planning.get_planning_request(request_id)
        from ..storage.planning_decision_store import PlanningDecisionStore
        return PlanningDecisionStore(self.store).get_planning_request(request_id)

    def get_mission(self, mission_id: str) -> Any:
        return self.store.get_mission(mission_id)

    def get_request_binding(self, request_id: str) -> Mapping[str, Any] | None:
        return self.planning.get_request_binding(request_id)

    def get_grant(self, grant_id: str, revision: int | None = None) -> Mapping[str, Any] | None:
        return self.planning.get_grant(grant_id, revision)


def build_planning_authorization(
    request_id: str,
    *,
    read: PlanningAuthorityReader,
    caller: PlanPrincipal,
    policy: PlanningLanePolicy,
    now_ms: int,
) -> PlanningAuthorizationSnapshot | SourceUnavailable:
    """Build one complete, read-only snapshot; no source is replaced by a default."""

    try:
        return _read_planning_authorization(
            request_id, read=read, caller=caller, policy=policy, now_ms=now_ms
        )
    except (sqlite3.Error, StoreError, json.JSONDecodeError):
        # An unreadable producer is distinct from a readable request that has
        # never received authority. Do not expose source contents to the caller.
        return SourceUnavailable("planning_authority", "authoritative source could not be read")


def _read_planning_authorization(
    request_id: str,
    *,
    read: PlanningAuthorityReader,
    caller: PlanPrincipal,
    policy: PlanningLanePolicy,
    now_ms: int,
) -> PlanningAuthorizationSnapshot | SourceUnavailable:

    request = read.get_planning_request(request_id)
    if request is None:
        return SourceUnavailable("planning_request", "request binding is unavailable")
    mission_value = getattr(request, "mission_id", None)
    if mission_value is None and isinstance(request, Mapping):
        mission_value = request.get("mission_id")
    if not mission_value:
        return SourceUnavailable("planning_request", "request has no mission identity")
    mission_id = str(mission_value)
    mission = read.get_mission(mission_id)
    if mission is None:
        return SourceUnavailable("mission", "mission record is unavailable")
    binding = read.get_request_binding(request_id)
    if binding is None:
        return SourceUnavailable(
            "request_authority_binding", "request has no authority binding",
            reason_code="AUTHORIZATION_REQUIRED",
        )
    tenant_id = str(getattr(mission, "tenant_id", ""))
    if any(
        (
            str(binding.get("mission_id")) != mission_id,
            str(binding.get("tenant_id")) != tenant_id,
        )
    ):
        return SourceUnavailable(
            "request_authority_binding", "request binding does not match mission"
        )
    if str(binding.get("scope_id")) != str(caller.scope_id) or str(
        binding.get("planner_principal_id")
    ) != str(caller.principal_id):
        return SourceUnavailable(
            "request_authority_binding", "caller is not the bound planner principal"
        )
    grant = read.get_grant(str(binding.get("grant_id")))
    if grant is None:
        return SourceUnavailable("planning_lane_grant", "bound grant lineage is unavailable")
    if int(grant.get("revision", -1)) != int(
        binding.get("grant_revision", -2)
    ) or str(grant.get("grant_hash")) != str(binding.get("grant_hash")):
        return SourceUnavailable(
            "planning_lane_grant",
            "bound grant revision is no longer current",
            reason_code="REQUEST_BINDING_STALE",
        )
    if str(grant.get("policy_hash")) != policy.policy_hash or str(
        binding.get("policy_hash")
    ) != policy.policy_hash:
        return SourceUnavailable(
            "planning_lane_policy",
            "planning policy identity changed",
            reason_code="REQUEST_BINDING_STALE",
        )
    return PlanningAuthorizationSnapshot(
        request_id=str(request_id), mission_id=mission_id, tenant_id=tenant_id,
        scope_id=str(grant["scope_id"]), planner_principal_id=str(grant["planner_principal_id"]),
        grant_id=str(grant["grant_id"]), grant_revision=int(grant["revision"]),
        grant_hash=str(grant["grant_hash"]), issuer_id=str(grant["issuer_id"]),
        issuer_command_id=str(grant["issuer_command_id"]),
        issuer_receipt_hash=str(grant["issuer_receipt_hash"]),
        allowed_decisions=tuple(grant["allowed_decisions"]), policy_hash=str(grant["policy_hash"]),
        not_before_ms=int(grant["not_before_ms"]), expires_at_ms=int(grant["expires_at_ms"]),
        active=bool(grant["active"]), checked_at_ms=int(now_ms),
    )


def check_planning_authorization(
    snapshot: PlanningAuthorizationSnapshot | SourceUnavailable,
    *,
    decision_key: str,
    now_ms: int,
) -> str | None:
    """Return a stable refusal code, or ``None`` when the grant permits the key."""

    if isinstance(snapshot, SourceUnavailable):
        return snapshot.reason_code
    if not snapshot.active or snapshot.not_before_ms > now_ms or snapshot.expires_at_ms <= now_ms:
        return "AUTHORIZATION_REQUIRED"
    if decision_key not in snapshot.allowed_decisions:
        return "AUTHORIZATION_REQUIRED"
    return None


__all__ = (
    "PLANNING_DECISIONS", "PLANNING_POLICY_VERSION", "PlanningAuthorityReader",
    "PlanningAuthorizationSnapshot", "PlanningLanePolicy", "SourceUnavailable",
    "StorePlanningAuthorityReader", "build_planning_authorization",
    "check_planning_authorization",
)
