"""I07: an ordinary legacy Planner request remains byte-stable across cold open."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

_LC2 = Path(__file__).resolve().parents[1] / "lc2"
if str(_LC2) not in sys.path:
    sys.path.insert(0, str(_LC2))

from legacy_fixture import orchestrator, planner  # noqa: E402

from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402


def _rows(connection, statement: str, parameters: tuple[object, ...]) -> tuple[tuple, ...]:
    """Preserve SQLite's stored values, including the original JSON text bytes."""

    return tuple(tuple(row) for row in connection.execute(statement, parameters))


def test_legacy_planner_request_is_byte_stable_and_budgeted_after_cold_reopen(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        warm_provider = RoleScriptedProvider({"planner": []})
        async with orchestrator(tmp_path, warm_provider) as warm:
            mission, intent = await planner(warm, "i07-legacy-cold-reopen")

            stored = warm.store.connection.execute(
                "SELECT config_json,input_hash,input_id,creation_key FROM dispatch_intents "
                "WHERE intent_id=?",
                (intent.intent_id,),
            ).fetchone()
            assert stored is not None
            config_bytes = str(stored["config_json"]).encode("utf-8")
            config = json.loads(config_bytes)
            assert config == dict(intent.config)
            assert "planning_package" not in config
            assert "planning_protocol" not in config
            assert "planning_protocol_version" not in config
            assert config["prompt_version"]
            assert isinstance(config["message"], dict) and config["message"]["content"]

            frozen_intent = (
                intent.intent_id,
                config_bytes,
                hashlib.sha256(config_bytes).hexdigest(),
                str(stored["input_hash"]),
                str(stored["input_id"]),
                str(stored["creation_key"]),
            )
            frozen_events = _rows(
                warm.store.connection,
                "SELECT seq,event_id,idempotency_key,type,trace_id,mission_id,task_id,"
                "attempt_id,actor_type,actor_id,payload_json,created_at,schema_version "
                "FROM events WHERE mission_id=? ORDER BY seq",
                (mission.id,),
            )
            assert frozen_events
            frozen_budget_accounts = _rows(
                warm.store.connection,
                "SELECT * FROM budget_accounts WHERE mission_id=? ORDER BY account_id",
                (mission.id,),
            )
            frozen_reservations = _rows(
                warm.store.connection,
                "SELECT * FROM budget_reservations WHERE mission_id=? ORDER BY reservation_id",
                (mission.id,),
            )
            account = warm.commit.ledger.account(mission_account(mission.id))
            assert account.reserved_tokens > 0
            assert frozen_reservations
            frozen_account = account.to_json()
            assert warm_provider.calls == 0

        cold_provider = RoleScriptedProvider({"planner": []})
        async with orchestrator(tmp_path, cold_provider, upgraded=True) as cold:
            reopened = cold.store.get_intent(frozen_intent[0])
            assert reopened is not None
            row = cold.store.connection.execute(
                "SELECT config_json,input_hash,input_id,creation_key FROM dispatch_intents "
                "WHERE intent_id=?",
                (frozen_intent[0],),
            ).fetchone()
            assert row is not None
            reopened_config_bytes = str(row["config_json"]).encode("utf-8")
            assert reopened_config_bytes == frozen_intent[1]
            assert hashlib.sha256(reopened_config_bytes).hexdigest() == frozen_intent[2]
            assert (
                str(row["input_hash"]),
                str(row["input_id"]),
                str(row["creation_key"]),
            ) == frozen_intent[3:]
            assert dict(reopened.config) == json.loads(frozen_intent[1])
            assert "planning_package" not in reopened.config
            assert "planning_protocol" not in reopened.config
            assert "planning_protocol_version" not in reopened.config

            reopened_events = _rows(
                cold.store.connection,
                "SELECT seq,event_id,idempotency_key,type,trace_id,mission_id,task_id,"
                "attempt_id,actor_type,actor_id,payload_json,created_at,schema_version "
                "FROM events WHERE mission_id=? ORDER BY seq",
                (mission.id,),
            )
            assert reopened_events[: len(frozen_events)] == frozen_events
            assert _rows(
                cold.store.connection,
                "SELECT * FROM budget_accounts WHERE mission_id=? ORDER BY account_id",
                (mission.id,),
            ) == frozen_budget_accounts
            assert _rows(
                cold.store.connection,
                "SELECT * FROM budget_reservations WHERE mission_id=? ORDER BY reservation_id",
                (mission.id,),
            ) == frozen_reservations
            assert cold.commit.ledger.account(mission_account(mission.id)).to_json() == frozen_account
            assert cold_provider.calls == 0

    asyncio.run(case())
