"""Startup cleanup uses real SQLite pools, with a controlled sweep failure.

No subprocesses or provider calls. These controls do not repeat OS reap acceptance.
"""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from graph_helpers7 import drive_to_running, graph_service

from agent_orchestrator.artifacts.workspace import WorkspaceCleanupIncomplete, WorkspaceManager
from agent_orchestrator.governance.budgets import BudgetLedger, UsageFact
from agent_orchestrator.orchestrator import event_handler
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import AssembledOrchestratorRuntime, OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.storage.store import Store
from simple_harness.agents.runtime import AgentRuntime


class NeverProvider:
    async def invoke(self, *args, **kwargs):
        raise AssertionError("startup cleanup must not call a provider")


@pytest.mark.parametrize("close_raises", [False, True])
def test_sweep_failure_closes_all_pools_preserves_unknown_and_original_error(
    tmp_path, monkeypatch, close_raises
):
    commit, mission, tasks = graph_service(tmp_path)
    attempt = drive_to_running(commit, tasks["A"])
    with commit.store.transaction():
        commit.ledger.import_usage(
            subject_id=attempt.id,
            mission_id=mission.id,
            facts=[UsageFact("old-unknown-charge", 0, 0, None, unknown=True)],
        )
    reservation = commit.ledger.reservation(attempt.id)
    assert reservation is not None and reservation["state"] == "RESERVED"
    assert reservation["reserved_tokens"] == 4000
    commit.store.close()
    provider = NeverProvider()
    orch = Orchestrator(
        OrchestratorConfig(evidence_root=tmp_path),
        profiles={
            key: RuntimeProfile(key, provider, "fixture-model") for key in ("default", "other")
        },
    )
    error = WorkspaceCleanupIncomplete([{"status": "unknown", "identity": "retained"}])
    captured = {}
    original_assemble = event_handler.assemble_orchestrator_runtime

    def assemble(*args, **kwargs):
        captured["assembled"] = original_assemble(*args, **kwargs)
        captured["store"] = orch.store
        return captured["assembled"]

    def fail_sweep(self, **kwargs):
        raise error

    monkeypatch.setattr(event_handler, "assemble_orchestrator_runtime", assemble)
    monkeypatch.setattr(WorkspaceManager, "sweep_exec_copies", fail_sweep)
    enter = AsyncMock(side_effect=AssertionError("runtime started before sweep completed"))
    monkeypatch.setattr(AgentRuntime, "__aenter__", enter)
    original_exit = AssembledOrchestratorRuntime.__aexit__
    exits = []

    async def close(self, *exc_info):
        exits.append(exc_info)
        await original_exit(self, *exc_info)
        if close_raises:
            raise RuntimeError("secondary close failure")

    monkeypatch.setattr(AssembledOrchestratorRuntime, "__aexit__", close)

    async def exercise():
        with pytest.raises(WorkspaceCleanupIncomplete) as raised:
            await asyncio.wait_for(orch.__aenter__(), 5)
        assert raised.value is error
        assert raised.value.reports == ({"status": "unknown", "identity": "retained"},)
        enter.assert_not_awaited()
        await asyncio.wait_for(orch.__aexit__(None, None, None), 5)
        assert len(exits) == 1  # Host's follow-up exit is harmless.
        assert exits[0][1] is error

    asyncio.run(exercise())
    assert orch._store is None and orch._assembled is None
    with pytest.raises(sqlite3.ProgrammingError):
        captured["store"].connection.execute("SELECT 1")
    pools = captured["assembled"].pools
    assert len(pools) == 2
    for pool in pools.values():
        runtime = pool.runtime._assembled
        assert not runtime.database.is_open
        assert not runtime.runtime._leases and not runtime.runtime._fences
    reopened = Store.open(tmp_path / "orchestrator.db")
    try:
        ledger = BudgetLedger(reopened)
        assert ledger.reservation(attempt.id) == reservation
        assert ledger.has_unknown_usage(attempt.id)
    finally:
        reopened.close()


def test_exit_closes_store_even_when_pool_close_fails_and_second_exit_is_noop(tmp_path):
    orch = Orchestrator(OrchestratorConfig(evidence_root=tmp_path), NeverProvider())
    store = Store.open(tmp_path / "orchestrator.db")
    error = RuntimeError("pool close failed")
    close = AsyncMock(side_effect=error)
    orch._store = store
    orch._assembled = SimpleNamespace(__aexit__=close)

    async def exercise():
        with pytest.raises(RuntimeError) as raised:
            await asyncio.wait_for(orch.__aexit__(None, None, None), 5)
        assert raised.value is error
        await asyncio.wait_for(orch.__aexit__(None, None, None), 5)

    asyncio.run(exercise())
    close.assert_awaited_once()
    with pytest.raises(sqlite3.ProgrammingError):
        store.connection.execute("SELECT 1")
