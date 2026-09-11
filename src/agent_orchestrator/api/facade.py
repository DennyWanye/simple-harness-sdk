# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P3.1 external control facade (the user's Phase3 plan §3.3–§3.4; host support S2).

``MissionControlV1`` is the one surface a product talks to — the Host desktop App calls it
in process.  Nothing here is a second state machine: every write goes through the
Orchestrator door (:meth:`Orchestrator.create_mission`), the Commit Service or the
Approval API, and every read is a projection of the orchestration library.

* **Strict fields** (P3.1-A06): a request names only the open fields; an unknown field is
  refused, a field this surface does not open (tool set, risk level, task kind, money or
  runtime budgets) is refused by name — nothing is silently dropped.
* **Persistent receipts** (P3.1-A03): the same idempotency key with the same body returns
  the first receipt; a different body under that key is a ``conflict``.
* **Ownership** (P3.1-A04): every object is checked against the caller's tenant; a
  foreign object and a missing one read the same (``not_found``, no id in the message).
* **One read** (P3.1-A05): a snapshot and its ``through_seq`` come from the same read
  transaction; event pages are bounded and gap-free after that cursor.
* **Content-addressed artifacts**: read by immutable id only, re-hashed before they are
  returned, bounded in size; a local path is never accepted.

The caller's :class:`Principal` is fixed when the facade is made (never from a model, an
envelope or a request field).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..artifacts.workspace import sha256_file
from ..governance.permissions import Principal
from ..observability.secrets import find_secrets
from ..orchestrator.action_commits import ActionCommitError
from ..orchestrator.commit_service import MissionConflict
from .approvals import ApprovalApi, ApprovalRequestError
from .missions import MissionRequestError

FACADE_VERSION = "mission-control-v1"
OPEN_FIELDS = frozenset(
    {
        "goal",
        "success_criteria",
        "idempotency_key",
        "budget",
        "stop_conditions",
        "untrusted_sources",
        "synthesis",
        "conflict_reserve_tokens",
        "workspace_seed",
    }
)
CLOSED_FIELDS = {
    "allowed_tools": "the tool set is the deployment's",
    "risk_level": "risk levels are set by the deployment",
    "task_kind": "not open on this surface",
}
OPEN_BUDGET = frozenset({"max_tokens", "max_attempts"})
CLOSED_BUDGET = frozenset(
    {"max_cost_micros", "max_runtime_seconds", "max_concurrency", "max_tool_calls"}
)
MAX_EVENT_PAGE = 200
MAX_ARTIFACT_BYTES = 256 * 1024
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
DECISIONS = ("approve", "reject", "review_pass", "review_fail", "arbitrate")
TAKEOVER_ACTIONS = ("stop", "retry_with_note")
NOT_FOUND = "no such object for this caller"


class FacadeError(ValueError):
    """A refused request; ``code`` is stable for products to map."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MissionControlV1:
    def __init__(self, orchestrator: Any, *, tenant_id: str, principal: Principal) -> None:
        if not str(tenant_id).strip():
            raise ValueError("a tenant is required")
        self._orchestrator = orchestrator
        self._tenant = str(tenant_id)
        self._approvals = ApprovalApi(
            orchestrator.commit, principal, deployment=orchestrator.config.deployment_policy
        )

    @property
    def _store(self) -> Any:
        return self._orchestrator.store

    # ------------------------------------------------------------ ownership
    def _mission(self, mission_id: object) -> Any:
        mission = self._store.get_mission(str(mission_id))
        if mission is None or mission.tenant_id != self._tenant:
            raise FacadeError("not_found", NOT_FOUND)
        return mission

    def _owner_of(self, target_id: object) -> Any:
        target = str(target_id)
        store = self._store
        if store.get_mission(target) is not None:
            return self._mission(target)
        approval = store.get_approval(target)
        if approval is not None:
            return self._mission(approval.get("mission_id"))
        task = store.get_task(target)
        if task is not None:
            return self._mission(task.mission_id)
        raise FacadeError("not_found", NOT_FOUND)

    @staticmethod
    def _clean(*texts: str) -> None:
        for text in texts:
            if text and find_secrets(text):
                raise FacadeError("secret_rejected", "the text looks like it contains a secret")

    # ------------------------------------------------------------ commands
    def create(self, command: Mapping[str, Any]) -> dict[str, Any]:
        request = self._strict(command)
        self._clean(
            str(request.get("goal", "")),
            *(str(c) for c in request.get("success_criteria", ()) or ()),
        )
        try:
            mission, created = self._orchestrator.create_mission(
                tenant_id=self._tenant, request=request
            )
        except MissionConflict as error:
            raise FacadeError(
                "conflict", "this idempotency_key already names a different request"
            ) from error
        except (MissionRequestError, ValueError) as error:
            raise FacadeError("invalid_request", str(error)) from error
        found = self._store.find_mission(self._tenant, mission.idempotency_key)
        return {
            "mission_id": mission.id,
            "created": created,
            "spec_hash": "" if found is None else found[1],
            "status": str(mission.status),
            "facade": FACADE_VERSION,
        }

    @staticmethod
    def _strict(command: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(command, Mapping):
            raise FacadeError("invalid_request", "a command must be an object")
        unknown = sorted(set(command) - OPEN_FIELDS - set(CLOSED_FIELDS))
        if unknown:
            raise FacadeError("invalid_request", f"unknown fields: {unknown}")
        closed = sorted(set(command) & set(CLOSED_FIELDS))
        if closed:
            reasons = "; ".join(f"{name}: {CLOSED_FIELDS[name]}" for name in closed)
            raise FacadeError(
                "invalid_request", f"fields not open to this surface: {closed} ({reasons})"
            )
        budget = command.get("budget", {})
        if not isinstance(budget, Mapping):
            raise FacadeError("invalid_request", "budget must be an object")
        unknown_budget = sorted(set(budget) - OPEN_BUDGET - CLOSED_BUDGET)
        if unknown_budget:
            raise FacadeError(
                "invalid_request",
                f"unknown budget fields: {['budget.' + k for k in unknown_budget]}",
            )
        closed_budget = sorted(set(budget) & CLOSED_BUDGET)
        if closed_budget:
            raise FacadeError(
                "invalid_request",
                f"budget fields not open to this surface: {['budget.' + k for k in closed_budget]}",
            )
        return dict(command)

    def cancel(self, mission_id: str) -> dict[str, Any]:
        mission = self._mission(mission_id)
        if str(mission.status) in TERMINAL:  # idempotent: an ended Mission is left as it is
            return {"mission_id": mission.id, "status": str(mission.status), "changed": False}
        updated = self._orchestrator.commit.cancel_mission(mission.id)
        return {"mission_id": mission.id, "status": str(updated.status), "changed": True}

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        reason: str = "",
        note: str = "",
        ruling: str = "",
        basis: str = "",
        nonce: str | None = None,
    ) -> dict[str, Any]:
        request = self._store.get_approval(str(request_id))
        if request is None:
            raise FacadeError("not_found", NOT_FOUND)
        self._mission(request.get("mission_id"))
        if decision not in DECISIONS:
            raise FacadeError("invalid_request", f"decision must be one of {list(DECISIONS)}")
        self._clean(reason, note, basis)
        try:
            if decision == "approve":
                result = self._approvals.approve(request_id, nonce=nonce)
            elif decision == "reject":
                if not reason.strip():
                    raise FacadeError("invalid_request", "a rejection needs a reason")
                result = self._approvals.reject(request_id, reason=reason, nonce=nonce)
            elif decision in ("review_pass", "review_fail"):
                result = self._approvals.review(
                    request_id,
                    verdict="pass" if decision == "review_pass" else "fail",
                    note=note,
                    nonce=nonce,
                )
            else:
                if not ruling or not basis.strip():
                    raise FacadeError(
                        "invalid_request", "an arbitration needs a ruling and a basis"
                    )
                result = self._approvals.arbitrate(
                    request_id, ruling=ruling, basis=basis, nonce=nonce
                )
        except ApprovalRequestError as error:
            raise FacadeError("invalid_request", str(error)) from error
        except ActionCommitError as error:
            raise FacadeError("refused", str(error)) from error
        decided = dict(result.get("request") or self._store.get_approval(str(request_id)) or {})
        return {
            "request_id": str(request_id),
            "request_state": decided.get("state"),
            "receipt_hash": result.get("receipt_hash"),
        }

    def takeover(self, task_id: str, action: str, *, basis: str, note: str = "") -> dict[str, Any]:
        task = self._store.get_task(str(task_id))
        if task is None:
            raise FacadeError("not_found", NOT_FOUND)
        self._mission(task.mission_id)
        if action not in TAKEOVER_ACTIONS:
            raise FacadeError("invalid_request", f"action must be one of {list(TAKEOVER_ACTIONS)}")
        if not basis.strip():
            raise FacadeError("invalid_request", "a takeover needs a basis")
        self._clean(basis, note)
        try:
            return dict(
                self._approvals.takeover(str(task_id), action=action, basis=basis, note=note)
            )
        except ActionCommitError as error:
            raise FacadeError("refused", str(error)) from error

    def comment(self, target_id: str, text: str) -> dict[str, Any]:
        self._owner_of(target_id)
        if not str(text).strip():
            raise FacadeError("invalid_request", "a comment needs text")
        self._clean(text)
        try:
            return dict(self._approvals.comment(str(target_id), text))
        except ApprovalRequestError as error:
            raise FacadeError("invalid_request", str(error)) from error

    # ------------------------------------------------------------ reads
    def missions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        store = self._store
        with store.read_view():
            mine = [m for m in store.list_missions() if m.tenant_id == self._tenant]
            mine.sort(key=lambda m: m.created_at, reverse=True)
            return [
                {
                    "mission_id": m.id,
                    "goal": m.goal[:120],
                    "status": str(m.status),
                    "stop_reason": m.stop_reason,
                    "created_at": m.created_at,
                    "pending_approvals": len(store.list_approvals(m.id, "PENDING")),
                }
                for m in mine[: max(1, min(int(limit), 200))]
            ]

    def snapshot(self, mission_id: str) -> dict[str, Any]:
        store = self._store
        with store.read_view():  # the snapshot and its cursor come from one read
            mission = self._mission(mission_id)
            snapshot = store.snapshot(mission.id)
            through = store.last_event_seq(mission.id)
        report = dict(mission.final_report or {})
        return {
            "mission_id": mission.id,
            "through_seq": through,
            "graph_version": int(report.get("graph_version") or 0),
            "state_version": mission.version,
            "snapshot": snapshot,
            "facade": FACADE_VERSION,
        }

    def events(self, mission_id: str, *, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise FacadeError("invalid_request", "after_seq must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_EVENT_PAGE
        ):
            raise FacadeError("invalid_request", f"limit must be between 1 and {MAX_EVENT_PAGE}")
        store = self._store
        with store.read_view():
            mission = self._mission(mission_id)
            rows = store.list_events(mission.id, after_seq=after_seq, limit=limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "mission_id": mission.id,
            "events": [{**event.to_json(), "seq": event.seq} for event in rows],
            "through_seq": rows[-1].seq if rows else after_seq,
            "has_more": has_more,
        }

    def approvals(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        if mission_id is not None:
            self._mission(mission_id)
            return self._approvals.list(mission_id)
        mine = {m.id for m in self._store.list_missions() if m.tenant_id == self._tenant}
        return [item for item in self._approvals.list(None) if item.get("mission_id") in mine]

    def artifact_read(self, artifact_id: str) -> dict[str, Any]:
        identifier = str(artifact_id)
        artifact = None
        if "/" not in identifier and "\\" not in identifier:  # an id, never a path
            artifact = self._store.get_artifact(identifier)
        if artifact is None:
            raise FacadeError("not_found", NOT_FOUND)
        self._mission(artifact.mission_id)
        from pathlib import Path

        path = Path(artifact.storage_uri)
        try:
            actual = sha256_file(path)
            data = path.read_bytes()
        except OSError as error:
            raise FacadeError("integrity_error", "the artifact's content is missing") from error
        if actual != artifact.content_hash:
            raise FacadeError(
                "integrity_error", "the artifact's content no longer matches its recorded hash"
            )
        truncated = len(data) > MAX_ARTIFACT_BYTES
        return {
            "artifact_id": artifact.id,
            "mission_id": artifact.mission_id,
            "path": artifact.path,
            "content_hash": artifact.content_hash,
            "size_bytes": len(data),
            "content": data[:MAX_ARTIFACT_BYTES].decode("utf-8", errors="replace"),
            "truncated": truncated,
        }


__all__ = ("FACADE_VERSION", "FacadeError", "MissionControlV1")
