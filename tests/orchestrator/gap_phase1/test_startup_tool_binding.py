"""Cold SDK startup may resume tools before the orchestrator loop calls recover."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "step02"))
from test_recovery_matrix import _provider
from test_single_task_closure import config, spec

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import AssembledOrchestratorRuntime
from agent_orchestrator.storage.store import InjectedCrash
from simple_harness.contracts import CallId
from simple_harness.tools.contracts import ToolCall


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["attempt", "critic"])
async def test_pending_agent_tools_and_audit_are_ready_before_sdk_starts(
    tmp_path, monkeypatch, kind
):
    provider = _provider()
    async with Orchestrator(config(tmp_path, lease_seconds=0.3), provider, owner="first") as first:
        mission = await first.submit_mission(spec("startup-tool-proof"))
        if kind == "critic":
            # The verifier can re-enter dispatch after the one-shot fault.
            # Keep losing only its Host receipt; the actual SDK turn persists.
            record_submitted = first.commit.record_submitted

            def lose_critic_receipt(intent_id, *, receipt):
                if first.store.get_intent(intent_id).kind == "critic":
                    raise InjectedCrash("lost-critic-submit-receipt")
                return record_submitted(intent_id, receipt=receipt)

            monkeypatch.setattr(first.commit, "record_submitted", lose_critic_receipt)
        first.arm_fault("after_submit", kind=kind)
        with pytest.raises(InjectedCrash):
            await first.run()
        intent = next(i for i in first.store.list_intents("AGENT_CREATED") if i.kind == kind)
    await asyncio.sleep(0.35)
    original = AssembledOrchestratorRuntime.__aenter__
    seen = []

    async def resume_at_startup(assembled):
        # This is the exact boundary where SDK startup can resume an old tool.
        result = await assembled.gateway.execute(
            ToolCall(
                CallId("startup-resumed-write"),
                "workspace_write_file" if kind == "attempt" else "workspace_read_file",
                {"path": "startup-proof.md", "content": "durably recorded"}
                if kind == "attempt" else {"path": "parse_kv.py"},
            ),
            {"run_id": intent.agent_id},
        )
        assert result.error_code is None, result
        proof = assembled.gateway.executed_lookup(f"{intent.agent_id}:startup-resumed-write")
        if kind == "attempt":
            assert proof is not None
        else:
            assert proof is None  # Critic reads are not Worker tool charges.
        assert assembled.gateway.knowledge_reader is not None
        seen.append(result)
        return await original(assembled)

    monkeypatch.setattr(AssembledOrchestratorRuntime, "__aenter__", resume_at_startup)
    async with Orchestrator(
        config(tmp_path, lease_seconds=0.3), provider, owner="second"
    ) as second:
        audit = second.store.get_tool_call(f"{intent.agent_id}:startup-resumed-write")
        if kind == "attempt":
            assert audit is not None and audit["outcome"] == "succeeded"
        else:
            assert audit is None
            call = second.assembled.gateway.calls[0]
            assert call["view"] == "verify" and call["outcome"] == "succeeded"
        await second.run()
        assert second.store.get_mission(mission.id).status is MissionStatus.COMPLETED
    assert len(seen) == 1
