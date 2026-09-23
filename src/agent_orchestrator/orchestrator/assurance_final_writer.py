# SPDX-License-Identifier: Apache-2.0
"""The unique final writer of an assured Mission (spec §7.1, handoff item 7).

An assured Mission is never completed by ``judge_mission``'s success tail. The
Mission judge records that the success criteria are met and *requests* a
closeout; the CLOSEOUT consumer re-derives readiness (adopted root
GoalResolution, current evidence, root Scope met, effects known, no open
intents / reservations / unknown usage, success judgment recorded) and, when
READY, calls :func:`finalize_assured_mission` inside its own commit
transaction. That call is the only place an assured Mission becomes
COMPLETED: it moves the original missions row, releases the terminal pools,
emits the original ``MissionCompleted``, moves the closeout row READY →
FINALIZED and requests the NOTIFY transport with the final event identity.

Every terminal write on the assured lane — this one and the original failure /
cancellation writers — ends with :func:`request_assured_notification`, so the
Host push chain always learns the final state from a durable event.
Nothing here fabricates readiness: a closeout that is not READY, a Mission
that is not ACTIVE, a judgment that is missing or a row that moved refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..assurance.codec import AssuranceError, canonical, fingerprint, integer, text
from ..assurance.refs import AssuranceRef, Pin
from ..contracts import Event, Mission, MissionStatus
from ..contracts.state_machines import MissionStopReason
from ..storage.assurance_store import AssuranceStore
from ..storage.htn_store import HtnStore
from .state_machine import next_mission

FINALIZED_KIND = "AssuranceMissionFinalized"
NOTIFICATION_EVENT = "AssuranceStatusNotificationRequested"
CLOSEOUT_REQUESTED_EVENT = "AssuranceCloseoutRequested"
JUDGMENT_KEY = "assurance_judgment"
CLOSEOUT_KEY = "assurance_closeout"


def is_assured(store: Any, mission_id: str) -> bool:
    """Whether this Mission runs on the assured lane; a missing contract is not."""
    try:
        return AssuranceStore(store).lane(mission_id) == "ASSURANCE_1_1"
    except AssuranceError:
        return False


def recorded_judgment(mission: Mission) -> Mapping[str, Any] | None:
    """The Mission judge's recorded success judgment awaiting closeout, if any."""
    report = mission.final_report or {}
    judgment = report.get(JUDGMENT_KEY)
    if not isinstance(judgment, Mapping) or judgment.get("met") is not True:
        return None
    return judgment


def assured_closeout_pending(store: Any, mission: Mission | None) -> bool:
    """An ACTIVE assured Mission whose success is judged and whose closeout is not final.

    The orchestrator loop neither re-judges nor stalls such a Mission: DRAINING /
    BLOCKED_UNKNOWN keep it ACTIVE until the original responsibilities converge
    (handoff item 7: no no-progress pseudo failure).
    """
    if mission is None or mission.status is not MissionStatus.ACTIVE:
        return False
    if recorded_judgment(mission) is None:
        return False
    return is_assured(store, mission.id)


def request_assured_closeout(
    commit: Any, mission: Mission, *, report: Mapping[str, Any], judged: Event
) -> Mission:
    """``judge_mission``'s assured success tail: record, request, stay ACTIVE.

    Called inside the judge's own transaction after ``MissionSuccessJudged``. The
    report the legacy tail would have completed the Mission with is kept on the
    row (the final writer completes from it), and the request is a durable
    event the CLOSEOUT consumer ingests.
    """
    store = commit._store
    if not store.connection.in_transaction:
        raise AssuranceError("FINAL_WRITER_TRANSACTION_REQUIRED")
    if mission.status is not MissionStatus.ACTIVE:
        raise AssuranceError("MISSION_NOT_ACTIVE")
    judgment = {
        "met": True,
        "judged_event_id": judged.id,
        "judged_at_version": int(mission.version),
    }
    pending = next_mission(mission, final_report={**dict(report), JUDGMENT_KEY: judgment})
    store.update_mission(pending, expected_version=mission.version)
    commit._emit(
        CLOSEOUT_REQUESTED_EVENT,
        mission.id,
        key=f"{mission.id}:{judged.id}",
        payload={**judgment, "mission_version": pending.version},
    )
    return pending


def request_assured_notification(
    commit: Any, mission_id: str, final: Event, *, state_version: int
) -> Event | None:
    """Emit the NOTIFY request for a terminal event of an assured Mission.

    Idempotent per final event (the event key); a legacy Mission gets nothing.
    """
    if not is_assured(commit._store, mission_id):
        return None
    return commit._emit(
        NOTIFICATION_EVENT,
        mission_id,
        key=f"{mission_id}:{final.id}",
        payload={
            "final_event_id": final.id,
            "state_version": integer(state_version),
            "final_event_type": final.type,
        },
    )


def finalize_assured_mission(
    commit: Any, mission_id: str, evaluation: Mapping[str, Any]
) -> AssuranceRef:
    """READY closeout → COMPLETED Mission + FINALIZED row, in the caller's transaction.

    ``evaluation`` is the CLOSEOUT consumer's re-evaluation that just landed
    READY under this same lock. Everything it says is re-read here from the
    rows before anything is written; a disagreement refuses (the work is
    rechecked, never repaired).
    """
    store = commit._store
    mission_id = text(mission_id)
    if not store.connection.in_transaction:
        raise AssuranceError("FINAL_WRITER_TRANSACTION_REQUIRED")
    if not is_assured(store, mission_id):
        raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
    if evaluation.get("mission_id") != mission_id or evaluation.get("state") != "READY":
        raise AssuranceError("CLOSEOUT_NOT_READY")
    connection = store.connection
    row = connection.execute(
        "SELECT * FROM assurance_closeouts WHERE mission_id=?", (mission_id,)
    ).fetchone()
    if row is None or row["state"] != "READY":
        raise AssuranceError("CLOSEOUT_NOT_READY")
    if row["resolution_id"] != evaluation.get("resolution_id"):
        raise AssuranceError("RECHECK_REQUIRED", "closeout resolution")
    mission = commit._require_mission(mission_id)
    if mission.status is not MissionStatus.ACTIVE:
        raise AssuranceError("MISSION_NOT_ACTIVE")
    if int(mission.version) != int(evaluation.get("mission_version", -1)):
        raise AssuranceError("RECHECK_REQUIRED", "mission version")
    judgment = recorded_judgment(mission)
    if judgment is None:
        raise AssuranceError("MISSION_JUDGMENT_MISSING")
    semantics = HtnStore(store)
    resolution = semantics.get_goal_resolution(row["resolution_id"])
    if resolution.mission_id != mission_id or str(resolution.verdict) != "ACCEPT":
        raise AssuranceError("RECHECK_REQUIRED", "root resolution")
    receipt_id = "assurance-finalized:" + mission_id
    if store.get_receipt(receipt_id) is not None:
        raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", "commit_receipts")
    now_ms = int(store.now * 1000)
    row_version = int(row["row_version"]) + 1
    report = dict(mission.final_report or {})
    report[CLOSEOUT_KEY] = {
        "resolution_id": str(resolution.resolution_id),
        "closeout_row_version": row_version,
        "check_body_hash": row["check_body_hash"],
        "finalized_receipt_id": receipt_id,
    }
    report.update(commit._ledger.usage_flags(mission_id))
    done = next_mission(
        mission,
        MissionStatus.COMPLETED,
        stop_reason=str(MissionStopReason.VERIFICATION_PASSED),
        final_report=report,
    )
    store.update_mission(done, expected_version=mission.version)
    commit._release_terminal_mission_pools(mission_id)
    final = commit._emit(
        "MissionCompleted",
        mission_id,
        key=mission_id,
        payload={"stop_reason": done.stop_reason, "final_report": report},
    )
    body = {
        "schema_version": 1,
        "mission_id": mission_id,
        "resolution_id": str(resolution.resolution_id),
        "closeout_row_version": row_version,
        "previous_check_body_hash": row["check_body_hash"],
        "judged_event_id": judgment["judged_event_id"],
        "final_event_id": final.id,
        "final_event_type": final.type,
        "state_version": int(done.version),
        "finalized_at_ms": now_ms,
    }
    digest = fingerprint(body)
    store.insert_receipt(
        commit_id=receipt_id,
        kind=FINALIZED_KIND,
        subject_id=mission_id,
        base_version=int(row["row_version"]),
        proposal_hash=digest,
        receipt=body,
    )
    check = {**dict(evaluation), "state": "FINALIZED", "finalized": body}
    moved = connection.execute(
        "UPDATE assurance_closeouts SET state='FINALIZED',row_version=?,check_body_hash=?,"
        "check_body_json=?,last_receipt_id=?,updated_at_ms=? WHERE mission_id=? AND row_version=?",
        (
            row_version,
            fingerprint(check),
            canonical(check),
            receipt_id,
            now_ms,
            mission_id,
            int(row["row_version"]),
        ),
    ).rowcount
    if moved != 1:
        raise AssuranceError("RECHECK_REQUIRED", "closeout row")
    ref = AssuranceRef("commit_receipt", Pin(receipt_id, 0, digest))
    commit._emit(
        FINALIZED_KIND,
        mission_id,
        key=receipt_id,
        payload={**body, "receipt_ref": ref.to_json()},
    )
    request_assured_notification(commit, mission_id, final, state_version=done.version)
    return ref


__all__ = (
    "CLOSEOUT_KEY",
    "CLOSEOUT_REQUESTED_EVENT",
    "FINALIZED_KIND",
    "JUDGMENT_KEY",
    "NOTIFICATION_EVENT",
    "assured_closeout_pending",
    "finalize_assured_mission",
    "is_assured",
    "recorded_judgment",
    "request_assured_closeout",
    "request_assured_notification",
)
