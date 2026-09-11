# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Approval API (ORCH §9.2 ``api/approvals.py``; original §22; plan D7-10 / D7-10').

List, approve, reject, revoke, comment, review, arbitrate, take over, and rule on an
UNKNOWN action.  The caller's authenticated identity is fixed when the API object is
made (``principal``): it never comes from a model parameter, an envelope field or a
workspace file, and no Agent tool reaches this module (S7-08).  Every text a person types
is checked for secrets at the door.  A candidate's ``reason`` was written by a model and is
shown marked as untrusted, so it cannot pass for the system's own words."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from ..governance.permissions import Principal
from ..governance.policies import DeploymentPolicy
from ..observability.secrets import find_secrets
from ..orchestrator.commit_service import CommitService

LIST_FIELDS = (
    "request_id",
    "kind",
    "mission_id",
    "task_id",
    "subject_key",
    "state",
    "level",
    "required_count",
    "grant_count",
    "expires_at",
    "reason",
    "topic",
    "options",
    "created_at",
    "closed_at",
)


class ApprovalRequestError(ValueError):
    """The request is refused at the API door (nothing was written)."""


class ApprovalApi:
    def __init__(
        self,
        commit: CommitService,
        principal: Principal,
        *,
        deployment: DeploymentPolicy | None = None,
    ) -> None:
        if not isinstance(principal, Principal):
            raise ApprovalRequestError("the API needs the caller's authenticated Principal")
        self._commit = commit
        self._principal = principal
        self._deployment = deployment or DeploymentPolicy()

    @staticmethod
    def _nonce(nonce: str | None) -> str:
        return nonce or uuid.uuid4().hex

    @staticmethod
    def _clean(*texts: str) -> None:
        for text in texts:
            if text and find_secrets(text):
                raise ApprovalRequestError("the text looks like it contains a secret; not accepted")

    # ------------------------------------------------------------ reading
    def list(
        self, mission_id: str | None = None, *, state: str | None = "PENDING"
    ) -> list[dict[str, Any]]:
        store = self._commit.store
        items = []
        for request in store.list_approvals(mission_id, *([state] if state else [])):
            item: dict[str, Any] = {name: request.get(name) for name in LIST_FIELDS}
            item["binding"] = request.get("binding")
            item["summary"] = request.get("summary")
            item["comments"] = list(request.get("comments", []))
            if request["kind"] == "action":
                action = store.get_action(str(request["subject_key"])) or {}
                item["action"] = {
                    "action_key": action.get("action_key"),
                    "version": action.get("version"),
                    "state": action.get("state"),
                    "connector": action.get("connector"),
                    "operation": action.get("operation"),
                    "target": action.get("target"),
                    "params": action.get("params"),
                    "params_hash": action.get("params_hash"),
                    "artifact_hash": action.get("artifact_hash"),
                    "reason": {"text": action.get("reason"), "source": "model (untrusted)"},
                }
            items.append(item)
        return items

    # ------------------------------------------------------------ decisions
    def approve(self, request_id: str, *, nonce: str | None = None) -> dict[str, Any]:
        request, receipt = self._commit.decide_approval(
            request_id,
            principal=self._principal,
            decision="grant",
            nonce=self._nonce(nonce),
            deployment=self._deployment,
        )
        return {"request": request, "receipt_hash": receipt}

    def reject(self, request_id: str, *, reason: str, nonce: str | None = None) -> dict[str, Any]:
        self._clean(reason)
        request, receipt = self._commit.decide_approval(
            request_id,
            principal=self._principal,
            decision="reject",
            nonce=self._nonce(nonce),
            deployment=self._deployment,
            reason=reason,
        )
        return {"request": request, "receipt_hash": receipt}

    def revoke(self, request_id: str, *, reason: str) -> dict[str, Any]:
        self._clean(reason)
        return self._commit.revoke_approval(request_id, principal=self._principal, reason=reason)

    def comment(self, target_id: str, text: str) -> dict[str, Any]:
        self._clean(text)
        return self._commit.add_comment(target_id, principal=self._principal, text=text).to_json()

    def review(
        self, request_id: str, *, verdict: str, note: str = "", nonce: str | None = None
    ) -> dict[str, Any]:
        self._clean(note)
        request, receipt = self._commit.review_result(
            request_id,
            principal=self._principal,
            verdict=verdict,
            note=note,
            nonce=self._nonce(nonce),
        )
        return {"request": request, "receipt_hash": receipt}

    def arbitrate(
        self, request_id: str, *, ruling: str, basis: str, nonce: str | None = None
    ) -> dict[str, Any]:
        self._clean(basis)
        return self._commit.arbitrate(
            request_id,
            principal=self._principal,
            ruling=ruling,
            basis=basis,
            nonce=self._nonce(nonce),
        )

    def takeover(self, task_id: str, *, action: str, basis: str, note: str = "") -> dict[str, Any]:
        self._clean(basis, note)
        return self._commit.takeover(
            task_id, principal=self._principal, action=action, basis=basis, note=note
        )

    def resolve_unknown(
        self, action_key: str, *, outcome: str, basis: str, evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._clean(basis, json.dumps(dict(evidence), ensure_ascii=False))
        return self._commit.override_action_outcome(
            action_key,
            principal=self._principal,
            outcome=outcome,
            basis=basis,
            evidence=evidence,
        )


__all__ = ("ApprovalApi", "ApprovalRequestError")
