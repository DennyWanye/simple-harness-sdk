# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-29 真机第八局：重启打断了执行者正在做的工具操作。

重启后那一轮挂在"工具结果未知"的等待上（执行层 wait blocker kind=tool）。编排层只认
kind=provider 时，这一轮既不结束也不算卡死（它报告"被阻塞"），尝试永远 RUNNING。现在
tool 与 provider 同样处理：等满时限后按"调用结果未知"放弃（不扣次数），换新尝试重做。
"""

from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.agent_worker import Liveness


def _live(kind):
    return Liveness(exists=True, state="running", blocked=True,
                    blocker=None if kind is None else {"kind": kind}, progress=3, settled=False)


def test_an_unknown_tool_effect_is_ended_like_an_unknown_provider_call():
    assert Orchestrator._provider_blocked(_live("provider"))
    assert Orchestrator._provider_blocked(_live("tool"))


def test_a_call_in_progress_or_a_slot_wait_is_not():
    assert not Orchestrator._provider_blocked(_live("provider_slot_wait"))
    assert not Orchestrator._provider_blocked(_live("provider_response_wait"))
    assert not Orchestrator._provider_blocked(_live(None))
    settled = Liveness(exists=True, state="failed", blocked=True, blocker={"kind": "tool"},
                       progress=3, settled=True)
    assert not Orchestrator._provider_blocked(settled)
