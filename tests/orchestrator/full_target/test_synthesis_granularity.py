# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""User decision 2026-09-26 ("强制但留余地"): coarse synthesised methods go back once.

Desktop thermos run: v9 of the synthesizer prompt still merged four chained deliverables
into one step.  On the first ask of a round, a step that must write three or more of
the Mission's ``file:`` criteria is sent back through the bounded synthesis retry;
the re-ask is admitted as written.  Two files in one step (code + test) never are.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch

CRITERIA = ("file:01-用户画像.md", "file:02-核心卖点.md", "file:03-推广节奏.md",
            "file:04-推广预算.md", "file:README.md", "说明结论")


def _coarse(links, criteria=CRITERIA):
    fake = SimpleNamespace(store=SimpleNamespace(
        get_mission=lambda _id: SimpleNamespace(success_criteria=criteria)))
    method = SimpleNamespace(composition=SimpleNamespace(criterion_links=[
        SimpleNamespace(parent_criterion_id=parent, child_step=step) for parent, step in links]))
    return HierarchicalDispatch._coarse_file_steps(fake, "m1", method)


def test_a_step_writing_three_or_more_files_is_coarse():
    links = [("c-user-1", "materials"), ("c-user-2", "materials"), ("c-user-3", "materials"),
             ("c-user-4", "materials"), ("c-user-5", "summary"), ("c-user-6", "summary")]
    assert _coarse(links) == [("materials", ["01-用户画像.md", "02-核心卖点.md", "03-推广节奏.md", "04-推广预算.md"])]


def test_one_step_per_file_or_a_code_and_test_pair_is_not():
    per_file = [(f"c-user-{n}", f"s{n}") for n in range(1, 6)]
    assert _coarse(per_file) == []
    pair = [("c-user-1", "impl"), ("c-user-2", "impl")]
    assert _coarse(pair, ("file:src/a.py", "file:tests/test_a.py")) == []
    # Non-file criteria never count toward the bound.
    free = [("c-user-1", "write"), ("c-user-2", "write"), ("c-user-3", "write")]
    assert _coarse(free, ("写出结论", "说明依据", "列出风险")) == []
