# SPDX-License-Identifier: Apache-2.0
"""Host real-model run 4 (2026-09-23, mission-7ad4aae9eafdba2d): an assured Attempt
whose provider turn failed before any usage fact made ``_settle_if_known`` raise the
Assurance settlement's BudgetError out of the loop, which then re-raised on every
round. The reservation is held, visible on the timeline, and the loop goes on."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MethodType

SDK_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SDK_ROOT / "scripts/assurance_seams"))


def test_assured_attempt_with_open_settlement_holds_its_reservation(tmp_path):
    from _assured_fixture import Orchestrator, AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, []) as rt:
            rt.orch._settle_if_known = MethodType(Orchestrator._settle_if_known, rt.orch)
            attempt = rt.store.get_attempt(rt.stored.envelope.attempt_id)
            assert attempt is not None
            before = rt.store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE mission_id=? AND type='ReservationHeld'", (rt.mission.id,)
            ).fetchone()[0]
            # The fixture Worker never ran in the AgentRuntime: its execution inventory
            # is open, exactly the state a failed provider turn leaves behind.
            rt.orch._settle_if_known(attempt)  # must not raise
            rows = rt.store.connection.execute(
                "SELECT payload_json FROM events WHERE mission_id=? AND type='ReservationHeld'", (rt.mission.id,)
            ).fetchall()
            assert len(rows) == before + 1
            assert "assurance_settlement_pending" in rows[-1][0]
            held = rt.store.connection.execute(
                "SELECT state FROM budget_reservations WHERE subject_id=?", (attempt.id,)
            ).fetchall()
            assert held and all(state[0] != "RELEASED" for state in held)

    asyncio.run(body())
