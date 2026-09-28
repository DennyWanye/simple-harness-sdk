# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-29（第 4 批）：主循环只处理有变化的任务。

真机：运行中后台 CPU 86%～178%，每轮把每个未结束任务（含昨天停住的旧任务）都完整处理一遍。
没有新事件（心跳不算）的任务跳过；每 10 秒无论如何全量处理一次，卡死检测等照常发生。
"""
from types import SimpleNamespace

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.orchestrator.event_handler import MISSION_RECHECK_SECONDS, Orchestrator

NO_LIMIT = SimpleNamespace(max_runtime_seconds=None)


def _mission(mission_id, status=MissionStatus.ACTIVE, budget=NO_LIMIT, created_at=1000.0):
    return SimpleNamespace(id=mission_id, status=status, budget=budget, created_at=created_at)


def _orch(events):
    clock = SimpleNamespace(now=1000.0)

    def execute(sql, params=()):
        rows = [seq for seq, mission, kind in events if kind != "HeartbeatReceived"]
        return SimpleNamespace(fetchone=lambda: (max(rows),) if rows else None)

    class Store:
        connection = SimpleNamespace(execute=execute)

        @property
        def now(self):
            return clock.now

    store = Store()
    orch = SimpleNamespace(store=store, _mission_marks={})
    for name in ("_event_cursor", "_missions_due", "_mark_quiet"):
        setattr(orch, name, getattr(Orchestrator, name).__get__(orch))
    return orch, clock


def test_missions_are_skipped_until_something_happens_or_the_recheck():
    events = [(1, "a", "TaskCreated"), (2, "b", "TaskCreated")]
    orch, clock = _orch(events)
    a = _mission("a")
    b = _mission("b")
    assert orch._missions_due([a, b], orch._event_cursor()) == {"a", "b"}  # 第一次：全部
    orch._mark_quiet({"a", "b"}, orch._event_cursor())
    assert orch._missions_due([a, b], orch._event_cursor()) == set()  # 都没变化：跳过
    events.append((3, "a", "HeartbeatReceived"))
    assert orch._missions_due([a, b], orch._event_cursor()) == set()  # 心跳不算变化
    events.append((4, "a", "AttemptSubmitted"))
    # 别的任务有事也要看：b 可能正等 a 让出的名额或额度（step09 并发演练抓到的）
    assert orch._missions_due([a, b], orch._event_cursor()) == {"a", "b"}
    orch._mark_quiet({"a", "b"}, orch._event_cursor())
    clock.now += MISSION_RECHECK_SECONDS
    assert orch._missions_due([a, b], orch._event_cursor()) == {"a", "b"}  # 到点全量一次（卡死检测、租约到期）


def test_a_new_mission_is_always_processed():
    orch, _ = _orch([(1, "c", "MissionCreated")])
    created = _mission("c", MissionStatus.CREATED)
    orch._mark_quiet({"c"}, orch._event_cursor())
    assert orch._missions_due([created], orch._event_cursor()) == {"c"}


def test_a_mission_past_its_time_limit_is_processed_at_once():
    """总时限到了不等 10 秒的全量轮：马上处理，才能按时停下并结清预留。"""
    orch, clock = _orch([(1, "d", "TaskCreated")])
    limited = _mission("d", budget=SimpleNamespace(max_runtime_seconds=2))
    orch._mark_quiet({"d"}, orch._event_cursor())
    assert orch._missions_due([limited], orch._event_cursor()) == set()
    clock.now += 2
    assert orch._missions_due([limited], orch._event_cursor()) == {"d"}


def test_an_event_written_during_the_round_wakes_the_mission_next_round():
    """审阅 2026-09-29：安静标记用本轮开始时的游标；本轮中途别处写的事件下一轮要看到。"""
    events = [(1, "a", "TaskCreated")]
    orch, _ = _orch(events)
    a = _mission("a")
    start = orch._event_cursor()
    assert orch._missions_due([a], start) == {"a"}
    events.append((2, "b", "VerificationFinished"))  # 处理 a 之后、本轮结束之前
    orch._mark_quiet({"a"}, start)
    assert orch._missions_due([a], orch._event_cursor()) == {"a"}
