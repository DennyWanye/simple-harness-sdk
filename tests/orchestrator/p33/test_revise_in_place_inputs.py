# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 UI 全量点击：续写交付步骤（输入端口 delivery = 输出端口 delivery）
拿到上游 summary.md 后必须能写出新版 summary.md；以前上游输入一律只读，文档任务
的续写步骤永远交不了，试到次数用完。普通只消费输入的步骤仍然只读。"""
from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.versioning import UpstreamInput
from agent_orchestrator.orchestrator import event_handler
from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _port(key, schema="s1"):
    return SimpleNamespace(port_key=key, schema_ref=schema)


@pytest.mark.parametrize("inputs,outputs,expected", [
    ((_port("delivery"),), (_port("delivery"),), True),
    ((_port("delivery"),), (_port("delivery"), _port("action_candidate")), True),
    ((_port("delivery"),), (_port("report"),), False),
    ((_port("delivery", "s1"),), (_port("delivery", "s2"),), False),
    ((), (_port("delivery"),), False),
])
def test_revise_in_place_is_every_input_port_also_an_output(monkeypatch, inputs, outputs, expected):
    import agent_orchestrator.storage.htn_store as htn_store
    monkeypatch.setattr(htn_store.HtnStore, "__init__", lambda self, store: None)
    monkeypatch.setattr(htn_store.HtnStore, "task_semantics_of",
                        lambda self, m, t: SimpleNamespace(input_ports=inputs, output_ports=outputs))
    assert Orchestrator._revises_its_inputs(SimpleNamespace(store=None), "m", "t") is expected


@pytest.mark.parametrize("revises,protected_paths", [(True, set()), (False, {"summary.md"})])
def test_upstream_inputs_of_a_revise_in_place_task_are_writable(revises, protected_paths):
    artifact = SimpleNamespace()
    fake = SimpleNamespace(
        _protected_seed=lambda mission, task: {},
        _source_files=lambda attempt: {},
        _revises_its_inputs=lambda mission_id, task_id: revises,
        _upstream_inputs=lambda attempt: [UpstreamInput("up", "summary.md", "h" * 64, "art-1")],
        store=SimpleNamespace(get_artifact=lambda artifact_id: artifact),
    )
    original = event_handler.read_verified
    event_handler.read_verified = lambda art: b"draft"
    try:
        protected = Orchestrator._protected_files(
            fake, SimpleNamespace(id="m", final_report={}), SimpleNamespace(id="t", outputs=()), object())
    finally:
        event_handler.read_verified = original
    assert set(protected) == protected_paths
