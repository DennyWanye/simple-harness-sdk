"""``agent.delegate`` on the native plane goes through ``NativeCreationService`` (BW01):
the original delegation ticket and kernel child launch stay, the child gets a durable
creation intent, a binding and an ACTIVE Session in the same activation, and the child's
own prepare / recall work because it has a Session."""

from __future__ import annotations

import asyncio

from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider, message_texts

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.tools.delegate import DELEGATE_TOOL_NAME, child_agent_id_for

NONCE = "验证码-7f3a"
MAIN = AgentConfig(name="main", instructions="你是主 Agent。复杂任务交给 agent_delegate。", model_profile_ref="p", tool_names=(DELEGATE_TOOL_NAME,))


def test_delegate_creates_the_child_through_the_native_creation_service(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(
            [
                (DELEGATE_TOOL_NAME, {"objective": "研究 X 并给出结论", "delegation_id": "d-1"}),  # main turn-1: delegate
                f"结论：Y。{NONCE}",  # child turn-1
                f"综合子 Agent 的结论：Y。{NONCE}",  # main turn-1 final
            ]
        )
        runtime = build(tmp_path, provider)
        async with runtime:
            main = await runtime.create(MAIN, creation_key="main", caller=trusted_caller())
            result = await main.ask("请完成复杂任务", input_id="req-1", timeout=10)
            assert result.public_output is not None and NONCE in result.public_output.content
            assert result.delegation_count == 1
            uow = runtime.uow
            connection = uow.database.connection
            child_id = child_agent_id_for(main.agent_id, "d-1")
            child = uow.read_agent_binding(child_id)
            assert child is not None and child.role == "child" and child.creation_key == f"delegation:{main.agent_id}:d-1"
            child_run = uow.read_run(child.run_id)
            assert child_run is not None and child_run.parent_run_id == main.run_id  # the original ticket path
            session = store.read_live_session(connection, child_id)
            assert session is not None and session.state == "ACTIVE" and session.agent_id == child_id
            intents = connection.execute("SELECT COUNT(*) FROM arp_creation_intents WHERE proposed_agent_id=?", (child_id,)).fetchone()[0]
            assert intents == 1
            # The child's provider request was composed by the native plane (a frozen manifest exists).
            manifests = connection.execute(
                "SELECT COUNT(*) FROM arp_context_requests WHERE original_request_key LIKE ?", (f"{child.run_id}:%",)
            ).fetchone()[0]
            assert manifests == 1
            delegation = uow.read_agent_delegation("d-1")
            assert delegation is not None and delegation.state == "settled"
            assert runtime._delegate.launches == 1 and provider.calls == 3
            assert NONCE not in "\n".join(message_texts(provider.requests[0]))

    asyncio.run(case())
