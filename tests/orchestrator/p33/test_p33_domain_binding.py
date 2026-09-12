# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.3 切片 A · 领域在创建 Mission 时冻结，并在闸门上生效（plan v3 D1、D9；验收 P33-09）。

冻结照 `policy_version_id` 的先例做：绑定表 + `MissionCreated` payload + 回放正式状态。
没有领域的旧 Mission 落回 `code-v1`，行为逐字不变（A07）。
"""

from __future__ import annotations

import pytest
from graph_helpers7 import DIAMOND, graph_service
from helpers_step07 import spec

from agent_orchestrator.governance.domains import CODE_DOMAIN, DOC_DOMAIN
from agent_orchestrator.orchestrator.commit_service import CommitRejected, MissionSpec
from agent_orchestrator.storage.store import Store


def _service(tmp_path, **overrides):
    return graph_service(tmp_path, **overrides)


# ---------------------------------------------------------------- 冻结


def test_p33_a14_a_mission_freezes_its_domain_at_creation(tmp_path) -> None:
    store = Store.open(tmp_path / "orchestrator.db")
    from agent_orchestrator.orchestrator.commit_service import CommitService

    service = CommitService(store)
    mission, _ = service.create_mission(spec("d-1", domain=DOC_DOMAIN))
    binding = store.get_mission_domain(mission.id)
    assert binding is not None
    assert binding["domain_id"] == DOC_DOMAIN
    # 快照是内容的一份副本，不是指向当前注册表的指针：回放只读它
    assert binding["json"]["id"] == DOC_DOMAIN
    assert "code_test" not in binding["json"]["default_policy"]


def test_p33_a15_a_mission_without_a_domain_binds_the_code_domain(tmp_path) -> None:
    from agent_orchestrator.orchestrator.commit_service import CommitService

    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    mission, _ = service.create_mission(spec("d-2"))
    assert store.get_mission_domain(mission.id)["domain_id"] == CODE_DOMAIN


def test_p33_a16_the_code_domain_does_not_change_the_spec_hash(tmp_path) -> None:
    """A07 的兼容钉子：升级后 Host 重发同一个请求，不能因为多了一个默认字段就冲突。"""

    plain = MissionSpec(
        goal="g", success_criteria=("file:a.md",), tenant_id="t", idempotency_key="k"
    )
    assert "domain" not in plain.to_json()
    assert MissionSpec(
        goal="g",
        success_criteria=("file:a.md",),
        tenant_id="t",
        idempotency_key="k",
        domain=CODE_DOMAIN,
    ).to_json() == plain.to_json()


def test_p33_a17_an_unknown_domain_is_refused_at_creation(tmp_path) -> None:
    from agent_orchestrator.orchestrator.commit_service import CommitService

    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    with pytest.raises((CommitRejected, KeyError)):
        service.create_mission(spec("d-3", domain="nope-v1"))


def test_p33_a18_mission_created_carries_the_domain(tmp_path) -> None:
    from agent_orchestrator.orchestrator.commit_service import CommitService

    store = Store.open(tmp_path / "orchestrator.db")
    service = CommitService(store)
    mission, _ = service.create_mission(spec("d-4", domain=DOC_DOMAIN))
    created = [e for e in store.list_events(mission.id) if e.type == "MissionCreated"]
    assert created and created[0].payload["domain_id"] == DOC_DOMAIN


# ---------------------------------------------------------------- 闸门 1：整图提案


def _doc_nodes(criteria):
    return [
        {
            "key": "A",
            "goal": "核对资料",
            "rationale": "报告需要它",
            "dependencies": [],
            "success_criteria": list(criteria),
            "verification_policy": ["format_check", "rule_check", "critic_review"],
            "allowed_tools": [],
            "budget": {"max_tokens": 10_000, "max_attempts": 2},
            "priority": 1.0,
        }
    ]


def test_p33_a19_gate1_whole_graph_proposal_refuses_a_pytest_criterion(tmp_path) -> None:
    # commit_task_graph 把 GraphRejected 包成 CommitRejected，断言落在真实契约上
    with pytest.raises(CommitRejected) as error:
        _service(
            tmp_path,
            key="g-doc",
            nodes=_doc_nodes(["pytest:tests/test_x.py"]),
            domain=DOC_DOMAIN,
            success_criteria=("file:REPORT.md",),
        )
    assert "pytest" in str(error.value) and "domain" in str(error.value)


def test_p33_a20_gate1_accepts_the_document_shape(tmp_path) -> None:
    service, mission, tasks = _service(
        tmp_path,
        key="g-doc-ok",
        nodes=_doc_nodes(["file:notes/a.md"]),
        domain=DOC_DOMAIN,
        success_criteria=("file:REPORT.md",),
    )
    assert len(tasks) == 1


def test_p33_a21_gate1_still_accepts_the_code_shape(tmp_path) -> None:
    """A07：code 领域的既有图一个字都不用改。"""

    service, mission, tasks = _service(tmp_path, key="g-code", nodes=DIAMOND)
    assert len(tasks) == len(DIAMOND)
