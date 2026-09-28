# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-29 第 5 批：要发布的文件在计划里必须有唯一的产出步骤。

Host 在每条 ``action:file_publish.publish:X`` 前补 ``file:X``；合成器必须把这条要求链接到
恰好一个步骤（写出 X 的那一步），否则第一次退回重写。系统发布时只从那一步取文件。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch

CRITERIA = ("写一个词频模块", "file:wordfreq.py", "action:file_publish.publish:wordfreq.py",
            "file:README.md", "action:file_publish.publish:README.md")


def _unclear(links, criteria=CRITERIA):
    fake = SimpleNamespace(store=SimpleNamespace(
        get_mission=lambda _id: SimpleNamespace(success_criteria=criteria)))
    method = SimpleNamespace(composition=SimpleNamespace(criterion_links=[
        SimpleNamespace(parent_criterion_id=parent, child_step=step) for parent, step in links]))
    return HierarchicalDispatch._publish_source_steps(fake, "m1", method)


def test_each_published_file_linked_to_exactly_one_step_is_clear():
    links = [("c-user-1", "module"), ("c-user-2", "module"), ("c-user-4", "readme")]
    assert _unclear(links) == []


def test_a_published_file_with_no_producer_or_two_producers_goes_back():
    missing = [("c-user-1", "module"), ("c-user-2", "module")]
    assert _unclear(missing) == ["README.md 链接到 0 个步骤"]
    twice = [("c-user-2", "module"), ("c-user-4", "readme"), ("c-user-4", "publish")]
    assert _unclear(twice) == ["README.md 链接到 2 个步骤（publish、readme）"]


def test_a_publish_without_a_file_requirement_is_not_checked_here():
    """旧任务或别的连接器：没有配套 file: 要求时不在定计划时检查，运行时按文件名回退。"""
    assert _unclear([("c-user-1", "write")], ("写一份报告", "action:file_publish.publish:r.md")) == []
