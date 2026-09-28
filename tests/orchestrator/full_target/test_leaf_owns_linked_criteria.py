# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Desktop 2026-09-26: a leaf owes only the criteria its method links to it.

Thermos Mission, five files, five steps: every step's Task row carried all five
``file:`` checks and the whole Mission goal, so the first step wrote all five files
(and each later step would have had to write them again to pass).  A leaf the
adopted method links to some criteria now carries only those, and its goal says so.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_read_only_leaf_policy import _binding  # noqa: E402

from agent_orchestrator.contracts.htn import SideEffectKind  # noqa: E402
from agent_orchestrator.orchestrator.accepted_outputs import owned_criteria  # noqa: E402
from agent_orchestrator.orchestrator.occurrence_tasks import (  # noqa: E402
    occurrence_criteria,
    scoped_goal,
)


def _leaf(refs):
    from dataclasses import replace

    return replace(_binding(side_effect=SideEffectKind.LOCAL_WRITE), requirement_refs=tuple(refs))


def test_an_owned_leaf_carries_only_its_criteria():
    leaf = _leaf(("c-user-1", "c-user-2", "c-user-3"))
    assert occurrence_criteria(leaf, None, owned=("c-user-2",)) == ("c-user-2",)


def test_an_unlinked_leaf_keeps_every_criterion():
    leaf = _leaf(("c-user-1", "c-user-2"))
    assert occurrence_criteria(leaf, None) == ("c-user-1", "c-user-2")
    # a link to a criterion the leaf does not owe changes nothing
    assert occurrence_criteria(leaf, None, owned=("c-other",)) == ("c-user-1", "c-user-2")


def test_the_scoped_goal_names_the_share_first():
    text = scoped_goal("整个任务", ("c-user-1", "file:01-画像.md"), ["写出画像"])
    assert text.startswith("本步骤只负责：01-画像.md")
    assert "写出画像" in text and "不要创建或改写" in text and text.endswith("整个任务")


def test_owned_criteria_pairs_links_with_the_bound_occurrences():
    links = (
        SimpleNamespace(child_step="a", parent_criterion_id="c-user-1", evidence_requirement="A"),
        SimpleNamespace(child_step="b", parent_criterion_id="c-user-2", evidence_requirement="B"),
        SimpleNamespace(child_step=None, parent_criterion_id="c-user-3", evidence_requirement="C"),
    )
    method = SimpleNamespace(composition=SimpleNamespace(criterion_links=links, finalizer_step="b"))
    draft = SimpleNamespace(
        instance_id="mi-1",
        method_ref=SimpleNamespace(method_id="m", version=1),
        child_bindings=(SimpleNamespace(slot_key="a", occurrence_id="occ-a"),
                        SimpleNamespace(slot_key="b", occurrence_id="occ-b")))
    network = SimpleNamespace(method_instances=(draft,), is_adopted=lambda _id: True)
    semantics = SimpleNamespace(get_method=lambda _id, _v: SimpleNamespace(contract=method))
    assert owned_criteria(network, semantics) == {
        "occ-a": [("c-user-1", "A")],
        "occ-b": [("c-user-2", "B"), ("c-user-3", "C")],
    }


def test_an_unlinked_leaf_does_not_take_a_file_another_step_was_given():
    """2026-09-29 第 5 批（审阅）：Host 为每个要发布的文件补了 file:X；方法把它链接给了别的
    步骤时，没被链接的步骤兜底不再承担它，否则要写出不归它的文件、反复"文件缺失"。"""

    def item(ref, statement):
        return SimpleNamespace(criterion_id=ref, statement=statement,
                               required_evidence_policy=SimpleNamespace(required_check_ids=()))

    requirements = SimpleNamespace(criteria=(item("c-user-1", "先调研"), item("c-user-2", "file:README.md")))
    leaf = _leaf(("c-user-1", "c-user-2"))
    assert occurrence_criteria(leaf, requirements, claimed={"c-user-2"}) == ("c-user-1",)
    # 没有别的步骤认领时照旧兜底（旧行为）
    assert occurrence_criteria(leaf, requirements) == ("c-user-1", "c-user-2", "file:README.md")
    # 被认领的普通内容要求照旧兜底：只排除写文件要求
    text_only = SimpleNamespace(criteria=(item("c-user-1", "先调研"), item("c-user-2", "说明结论")))
    assert occurrence_criteria(leaf, text_only, claimed={"c-user-2"}) == ("c-user-1", "c-user-2")
