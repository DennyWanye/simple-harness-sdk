# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Approvals bound to one action version (ORCH-BUILD §9.2 permissions row; original
§17.3, §21.3, §22; plan D7-4).

An approval request names exactly what may happen: the Mission, the Task, the business
action id and its version, the parameter hash and the candidate artifact's hash, with an
expiry.  Changing any of them makes a new version and the old approval is superseded.
A decision can only come from a ``Principal`` the caller of the API supplies — a human
identity the orchestrator is given, never something a model wrote.  Each decision carries
a nonce; its receipt hash covers the binding, the principal, the decision and the nonce,
so a replayed receipt is recognised and never counted twice.  The approval itself is the
short-lived capability for that one action version (plan §6.1)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

BINDING_FIELDS = (
    "mission_id",
    "task_id",
    "action_id",
    "version",
    "params_hash",
    "artifact_hash",
)


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated human decision maker (supplied by the API caller)."""

    principal_id: str
    display: str = ""
    kind: str = "human"

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("a principal needs an id")
        if self.kind != "human":
            raise ValueError("only a human principal can decide an approval (original §22)")

    def to_json(self) -> dict[str, Any]:
        return {"principal_id": self.principal_id, "display": self.display, "kind": self.kind}


def binding_of(action: Mapping[str, Any]) -> dict[str, Any]:
    return {name: action.get(name) for name in BINDING_FIELDS}


def binding_matches(request: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    return dict(request.get("binding") or {}) == binding_of(action)


def decision_receipt_hash(
    *, request_id: str, binding: Mapping[str, Any], principal_id: str, decision: str, nonce: str
) -> str:
    body: dict[str, Any] = {
        "request_id": request_id,
        "binding": dict(binding),
        "principal_id": principal_id,
        "decision": decision,
        "nonce": nonce,
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def required_approvals(level: str) -> int:
    """Original §22: L2 needs one approval, L3 a double approval; L0/L1 run automatically."""

    return {"L0": 0, "L1": 0, "L2": 1, "L3": 2}.get(level, 2)


__all__ = (
    "BINDING_FIELDS",
    "Principal",
    "binding_matches",
    "binding_of",
    "decision_receipt_hash",
    "required_approvals",
)
