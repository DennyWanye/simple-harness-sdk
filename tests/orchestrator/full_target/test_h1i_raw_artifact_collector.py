"""I01 raw-reply artifact atomicity and replay regressions.

These cases extend the decode-only collector coverage without replacing its real
production-entry fixture: every call reaches ``Orchestrator._collect_plan_decision``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_h1i_decode_only_collector import _raw_fixture
from test_h1i_production_entry import _config, _open_planner_round, _seed_new_protocol

from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.decision_codec import (
    PlanningDecisionCodecError,
    parse_planning_decision,
)
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import StoreConflict
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _sql_snapshot(loop: Orchestrator, mission_id: str) -> tuple[int, tuple[tuple[Any, ...], ...]]:
    """The collector-owned persistence surface, including the answering intent."""

    rows: list[tuple[Any, ...]] = []
    for query in (
        "SELECT * FROM planning_decisions ORDER BY decision_id",
        "SELECT * FROM planning_requests ORDER BY request_id",
        "SELECT * FROM events WHERE mission_id = ? ORDER BY created_at, event_id",
        "SELECT * FROM dispatch_intents WHERE mission_id = ? ORDER BY intent_id",
    ):
        parameters: tuple[str, ...] = (mission_id,) if "mission_id = ?" in query else ()
        rows.extend(tuple(row) for row in loop.store.connection.execute(query, parameters))
    return loop.store.connection.total_changes, tuple(rows)


def _malformed_decode_only_raw(package: dict[str, Any]) -> str:
    """Name the demoted REPAIR branch but break a strict quadruple, not its tag."""

    raw = _raw_fixture("repair-propose-successor", package)
    body = json.loads(raw.removeprefix("<planning_decision>").removesuffix("</planning_decision>"))
    body["payload"]["old_task_ref"]["content_hash"] = "not-a-sha256"
    return "<planning_decision>" + json.dumps(body, ensure_ascii=False) + "</planning_decision>"


def test_malformed_decode_only_reply_is_unreadable_but_preserves_exact_raw_artifact(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="i01-malformed-decode-only"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            raw = _malformed_decode_only_raw(opener.config["planning_package"])
            with pytest.raises(PlanningDecisionCodecError):
                parse_planning_decision(raw)

            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)

            row = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert row is not None
            assert row["status"] == str(PlanningDecisionStatus.UNREADABLE)
            assert "DECISION_NOT_ENABLED_IN_PHASE" not in row["rejection_codes"]
            assert row["raw_artifact_ref"]
            assert loop.assembled.workspaces.artifact_store.read(
                row["raw_artifact_ref"]
            ) == raw.encode("utf-8")

    asyncio.run(case())


def test_raw_artifact_write_failure_leaves_no_collector_db_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="i01-raw-artifact-failure"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            raw = _raw_fixture("repair-propose-successor", opener.config["planning_package"])
            before = _sql_snapshot(loop, mission.id)

            def disk_full(_data: bytes) -> str:
                raise OSError("simulated artifact store failure")

            monkeypatch.setattr(loop.assembled.workspaces.artifact_store, "put_bytes", disk_full)
            with pytest.raises(OSError, match="simulated artifact store failure"):
                await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)

            assert _sql_snapshot(loop, mission.id) == before
            assert (
                PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                    opener.intent_id, 0
                )
                is None
            )

    asyncio.run(case())


def test_terminal_replay_never_writes_raw_artifact_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="i01-terminal-raw-replay"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            raw = _raw_fixture("bind-existing-goal-reuse", opener.config["planning_package"])
            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
            terminal = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert terminal is not None
            assert terminal["status"] == str(PlanningDecisionStatus.REJECTED)
            before = _sql_snapshot(loop, mission.id)

            def must_not_write(_data: bytes) -> str:
                raise AssertionError("terminal replay must not write CAS")

            monkeypatch.setattr(
                loop.assembled.workspaces.artifact_store, "put_bytes", must_not_write
            )
            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
            assert _sql_snapshot(loop, mission.id) == before

            with pytest.raises(StoreConflict):
                await loop._collect_plan_decision(opener, object(), mission, raw + "\n", dispatch)
            assert _sql_snapshot(loop, mission.id) == before

    asyncio.run(case())
