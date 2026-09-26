# SPDX-License-Identifier: Apache-2.0
"""Persist clock observations through the original Commit and event writers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from ..assurance.clock import ClockState
from ..assurance.codec import AssuranceError, fingerprint, integer
from ..assurance.refs import AssuranceRef, Pin
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic

if TYPE_CHECKING:
    from .commit_service import CommitService


# 2026-09-26 (Host 真机): the high-water mark moves on every observation, so "one
# receipt per changed observation" wrote a receipt on every tick — 475k rows in two
# days, and every root check scans that table, pinning the event loop at 100% CPU.
# A STABLE observation that only advances the high-water mark is persisted at most
# once per interval; generation/state changes are always persisted at once.
CLOCK_PERSIST_INTERVAL_MS = 60_000


def observe_assurance_clock(commit: CommitService, *, now_ms: int) -> ClockState:
    """One receipt per meaningful change; one discontinuity per generation.

    Events are diagnostic wakeups. They never assert source validity or cancel
    physical work. A rollback does not move any expiry or work deadline.  The
    returned state always reflects ``now_ms``; only its persistence is coarse
    (see :data:`CLOCK_PERSIST_INTERVAL_MS`), so a rollback shorter than that
    interval against the persisted mark is not recorded as a discontinuity.
    """
    integer(now_ms)
    store = commit.store
    with atomic(store) as connection:
        row = connection.execute(
            "SELECT * FROM assurance_environment_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise AssuranceError("ASSURANCE_ENVIRONMENT_UNINITIALIZED")
        previous = ClockState(row["clock_generation"], row["wall_high_ms"], row["clock_state"])
        current = previous.observe(now_ms)
        if current == previous:
            return current
        if (
            current.state == previous.state == "STABLE"
            and current.generation == previous.generation
            and current.wall_high_ms - previous.wall_high_ms < CLOCK_PERSIST_INTERVAL_MS
        ):
            return current
        receipt_id = "assurance-clock:" + uuid4().hex
        body = {
            "schema_version": 1,
            "receipt_role": "CLOCK_OBSERVATION_ONLY",
            "observed_at_ms": now_ms,
            "previous_row_version": row["row_version"],
            "clock_generation": current.generation,
            "wall_high_ms": current.wall_high_ms,
            "clock_state": current.state,
        }
        receipt_hash = fingerprint(body)
        store.insert_receipt(
            commit_id=receipt_id,
            kind="AssuranceClockObserved",
            subject_id="assurance-environment",
            base_version=row["row_version"],
            proposal_hash=receipt_hash,
            receipt=body,
        )
        AssuranceStore(store).observe_clock(
            now_ms, receipt=AssuranceRef("commit_receipt", Pin(receipt_id, 0, receipt_hash))
        )
        if current.state != previous.state:
            event_type = (
                "TimeDiscontinuity" if current.state == "ROLLBACK" else "AssuranceClockStable"
            )
            for binding in connection.execute(
                "SELECT mission_id FROM assurance_mission_bindings ORDER BY mission_id"
            ).fetchall():
                commit._emit(
                    event_type,
                    binding["mission_id"],
                    key=f"assurance:{binding['mission_id']}:{current.generation}:{current.state}",
                    payload={**body, "clock_receipt_id": receipt_id},
                )
        return current
