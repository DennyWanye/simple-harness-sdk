# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-29 真机第八局：重启打断了执行者正在做的工具操作。

重启后那一轮挂在"工具结果未知"的等待上（执行层 wait blocker kind=tool）。编排层只认
kind=provider 时，这一轮既不结束也不算卡死（它报告"被阻塞"），尝试永远 RUNNING。现在
tool 与 provider 同样处理：等满时限后按"调用结果未知"放弃（不扣次数），换新尝试重做。
"""

from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.agent_worker import Liveness


def _live(kind, **extra):
    return Liveness(exists=True, state="running", blocked=True,
                    blocker=None if kind is None else {"kind": kind, **extra}, progress=3, settled=False)


def test_an_unknown_tool_effect_is_ended_like_an_unknown_provider_call():
    assert Orchestrator._provider_blocked(_live("provider"))
    assert Orchestrator._provider_blocked(_live("tool", effect_state="unknown"))


def test_a_tool_that_is_still_running_is_not_ended():
    """正在执行的工具（例如跑测试）也报告 kind=tool；它的状态不是 unknown，不能被当成结果未知。"""
    assert not Orchestrator._provider_blocked(_live("tool", effect_state="handed_off"))
    assert not Orchestrator._provider_blocked(_live("tool"))


def test_a_call_in_progress_or_a_slot_wait_is_not():
    assert not Orchestrator._provider_blocked(_live("provider_slot_wait"))
    assert not Orchestrator._provider_blocked(_live("provider_response_wait"))
    assert not Orchestrator._provider_blocked(_live(None))
    settled = Liveness(exists=True, state="failed", blocked=True, blocker={"kind": "tool"},
                       progress=3, settled=True)
    assert not Orchestrator._provider_blocked(settled)


def test_the_worker_bridge_reports_the_tool_effects_own_state():
    """执行层上报工具阻塞时带上该工具操作的持久状态（进行中 vs unknown），编排层才分得清。"""
    from types import SimpleNamespace

    from agent_orchestrator.runtime.agent_worker import AgentBridge

    states = {"effect-a": "unknown", "effect-b": "handed_off"}

    def read_effect(effect_id):
        state = states.get(str(effect_id))
        return None if state is None else SimpleNamespace(state=SimpleNamespace(value=state))

    worker = SimpleNamespace(_runtime=SimpleNamespace(uow=SimpleNamespace(read_effect=read_effect)))
    enrich = AgentBridge._with_effect_state.__get__(worker)
    assert enrich({"kind": "tool", "ledger_identity": "effect-a"})["effect_state"] == "unknown"
    assert enrich({"kind": "tool", "ledger_identity": "effect-b"})["effect_state"] == "handed_off"
    assert "effect_state" not in enrich({"kind": "tool", "ledger_identity": "effect-missing"})
    assert enrich({"kind": "provider", "ledger_identity": "x"}) == {"kind": "provider", "ledger_identity": "x"}
