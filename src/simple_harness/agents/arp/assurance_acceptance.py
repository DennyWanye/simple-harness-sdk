# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The official EvaluationAcceptance reader over the Assurance 1.1 ledger (BW09).

``AssuranceSkillAcceptance`` implements ``SkillAcceptancePort`` for a deployment
whose Assurance line is installed: a Skill evaluation is accepted only when the
Host dispatched it as an original Assurance Mission (the catalogue's dispatch link,
``SkillLifecycleService.record_evaluation_dispatch``) and that Mission's original
acceptance writer committed a USABLE ``ACCEPT`` use certificate for the evaluated
Result, together with its ``AssuranceUseCertified`` receipt and its ``acceptances``
row.  The ``acceptance_ref`` pin names that certificate: ``id`` = certificate id,
``revision`` = 0, ``content_hash`` = the certificate's own hash.

The tie between the evaluation and the Mission is not a Host assertion alone: the
Host must create the evaluation Mission with ``idempotency_key ==
evaluation_mission_key(evaluation)`` (the orchestrator stores it immutably per
tenant), the catalogue's dispatch link names the same Mission and its evaluated task,
and the certificate and the acceptance must have been issued *after* the dispatch
was recorded, so an older acceptance can never be linked after the fact.

Nothing here signs a PASS: every path that cannot prove the chain raises a named
error, and the reader never writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .errors import ArpError
from .pins import Pin

ASSURANCE_1_1 = "ASSURANCE_1_1"
USE_CERTIFIED_KIND = "AssuranceUseCertified"
ACCEPT_PURPOSE = "ACCEPT"
ACCEPTANCE_CONSUMER = "ACCEPTANCE"


def evaluation_mission_key(evaluation: Pin) -> str:
    """The ``idempotency_key`` the Host must create the evaluation Mission with."""

    evaluation.require_kind("evaluation")
    return f"skill-eval:{evaluation.id}@{evaluation.revision}:{evaluation.content_hash[:16]}"


def _incomplete(reason: str, **detail: Any) -> ArpError:
    return ArpError(
        "SKILL_EVALUATION_INCOMPLETE",
        f"official evaluation acceptance not established: {reason}",
        detail={"successor": ASSURANCE_1_1, "reason": reason, **detail},
    )


@dataclass(slots=True)
class AssuranceSkillAcceptance:
    """Read-only ``SkillAcceptancePort`` over the orchestrator ``Store`` that holds the
    Assurance ledger.  ``root_incarnation`` (when given) is the current execution root
    reader, normally ``AssuranceRootGate.require_execution``; a certificate issued under
    another root is refused, a restored root has to re-authorize."""

    store: Any
    clock_ms: Callable[[], int]
    root_incarnation: Callable[[], Any] | None = None
    dispatches: Callable[[Pin], Mapping[str, Any] | None] | None = field(default=None, repr=False)
    successor: str = ASSURANCE_1_1

    def bind_lifecycle(self, lifecycle: Any) -> None:
        self.dispatches = lifecycle.dispatch_for

    # ---- port -------------------------------------------------------------------------------

    def verify(self, binding: Mapping[str, Any], acceptance_ref: Pin) -> Mapping[str, Any]:
        acceptance_ref.require_kind("acceptance")
        evaluation = Pin.from_json(binding["evaluation_ref"])
        if self.dispatches is None:
            raise ArpError("SOURCE_UNAVAILABLE", "acceptance reader is not bound to a skill catalogue")
        dispatch = self.dispatches(evaluation)
        if dispatch is None:
            raise _incomplete("EVALUATION_NOT_DISPATCHED", evaluation_ref=evaluation.to_json())
        if acceptance_ref.revision != 0:
            raise ArpError("SOURCE_HASH_CONFLICT", "a use certificate has exactly one revision (0)")
        if type(dispatch.get("task_id")) is not str or not dispatch["task_id"]:
            raise _incomplete("DISPATCH_TASK_MISSING", evaluation_ref=evaluation.to_json())
        recorded_at_ms = int(dispatch["recorded_at_ms"])
        expected_key = evaluation_mission_key(evaluation)
        with self.store.read_view() as connection:
            mission = connection.execute("SELECT idempotency_key, created_at FROM missions WHERE mission_id=?", (dispatch["mission_id"],)).fetchone()
            if mission is None:
                raise _incomplete("MISSION_MISSING", mission_id=dispatch["mission_id"])
            if str(mission[0]) != expected_key:
                raise _incomplete("MISSION_KEY_MISMATCH", mission_id=dispatch["mission_id"], expected_idempotency_key=expected_key)
            row = connection.execute(
                "SELECT certificate_id, mission_id, consumer_kind, consumer_id, purpose, certificate_hash, certificate_json, issued_at_ms, not_after_ms "
                "FROM assurance_use_certificates WHERE certificate_id=?",
                (acceptance_ref.id,),
            ).fetchone()
            if row is None:
                raise _incomplete("CERTIFICATE_MISSING", certificate_id=acceptance_ref.id)
            certificate_id, mission_id, consumer_kind, consumer_id, purpose, certificate_hash, certificate_json, issued_at_ms, not_after_ms = row
            if str(certificate_hash) != acceptance_ref.content_hash:
                raise ArpError("SOURCE_HASH_CONFLICT", "acceptance_ref does not hash the committed certificate")
            receipt = connection.execute(
                "SELECT kind, receipt_json FROM commit_receipts WHERE commit_id=?", ("assurance-use-certified:" + str(certificate_id),)
            ).fetchone()
            if receipt is None or str(receipt[0]) != USE_CERTIFIED_KIND:
                raise _incomplete("CERTIFICATE_RECEIPT_MISSING", certificate_id=str(certificate_id))
            receipt_body = json.loads(str(receipt[1]))
            if receipt_body.get("certificate_hash") != str(certificate_hash) or receipt_body.get("mission_id") != str(mission_id):
                raise ArpError("SOURCE_HASH_CONFLICT", "the certificate receipt names another certificate")
            record_id = str(receipt_body.get("record_id"))
            acceptance = connection.execute(
                "SELECT acceptance_id, task_id, validity, accepted_at_ms FROM acceptances WHERE mission_id=? AND review_record_id=?",
                (str(mission_id), record_id),
            ).fetchone()
        if str(purpose) != ACCEPT_PURPOSE or str(consumer_kind) != ACCEPTANCE_CONSUMER:
            raise _incomplete("NOT_AN_ACCEPTANCE_CERTIFICATE", purpose=str(purpose), consumer_kind=str(consumer_kind))
        if str(mission_id) != dispatch["mission_id"]:
            raise _incomplete("MISSION_MISMATCH", certificate_mission_id=str(mission_id), dispatched_mission_id=dispatch["mission_id"])
        if acceptance is None:
            raise _incomplete("ACCEPTANCE_ROW_MISSING", record_id=record_id)
        acceptance_id, task_id, validity, accepted_at_ms = (str(acceptance[0]), str(acceptance[1]), str(acceptance[2]), int(acceptance[3]))
        if task_id != dispatch["task_id"]:
            raise _incomplete("TASK_MISMATCH", accepted_task_id=task_id, dispatched_task_id=dispatch["task_id"])
        if str(consumer_id) != acceptance_id:
            raise ArpError("SOURCE_HASH_CONFLICT", "the certificate's consumer is not this acceptance")
        if validity != "CURRENT":
            raise _incomplete("ACCEPTANCE_" + validity, acceptance_id=acceptance_id)
        if int(issued_at_ms) < recorded_at_ms or accepted_at_ms < recorded_at_ms:
            raise _incomplete("ACCEPTANCE_BEFORE_DISPATCH", issued_at_ms=int(issued_at_ms), accepted_at_ms=accepted_at_ms, dispatch_recorded_at_ms=recorded_at_ms)
        body = json.loads(str(certificate_json))
        if body.get("decision") != "USABLE":
            raise _incomplete("CERTIFICATE_" + str(body.get("decision")), certificate_id=str(certificate_id))
        now = self.clock_ms()
        if now < int(issued_at_ms) or (not_after_ms is not None and now >= int(not_after_ms)):
            raise _incomplete("CERTIFICATE_EXPIRED", certificate_id=str(certificate_id), not_after_ms=not_after_ms)
        if self.root_incarnation is not None:
            current = self.root_incarnation()
            current_id = getattr(current, "root_incarnation_id", current)
            if body.get("root_incarnation_id") != current_id:
                raise _incomplete("ROOT_CHANGED", certificate_root=body.get("root_incarnation_id"))
        return {
            "accepted": True,
            "successor": self.successor,
            "evaluation_ref": dict(binding["evaluation_ref"]),
            "acceptance_ref": acceptance_ref.to_json(),
            "mission_id": str(mission_id),
            "task_id": task_id,
            "acceptance_id": acceptance_id,
            "accepted_at_ms": accepted_at_ms,
            "dispatch_recorded_at_ms": recorded_at_ms,
            "record_id": record_id,
            "consumer_id": str(consumer_id),
            "certificate_hash": str(certificate_hash),
            "policy_ref": body.get("policy_ref"),
            "issued_at_ms": int(issued_at_ms),
            "not_after_ms": None if not_after_ms is None else int(not_after_ms),
        }


__all__ = ("ASSURANCE_1_1", "AssuranceSkillAcceptance", "evaluation_mission_key")
