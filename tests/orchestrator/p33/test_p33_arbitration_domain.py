# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.3 切片 A · 仲裁这条路上的两处 pytest 硬编码（plan v3 D1、D6；验收 P33-35a 的前置）。

第 1 轮评审查出 `conflict_task` 的准则写死了探针测试；第 2 轮又查出**还有两处**：
`check_arbitration` 要求仲裁 claim 必须引 `pytest:` 且落在 `arbitration/<key>/` 下，
以及 `_open_conflict` 写死「`code_test` 没部署就 defer」——后者意味着一个纯文档部署里
**每一个冲突都开不出仲裁任务**，A05 在那种部署下是死的。

文档领域没有可确定判定的外部检查（只能证明"某来源某段这么写"，不能证明"哪一方对"），
所以它的裁决层是 `human_review`，不假装有一个外部检查层。
"""

from __future__ import annotations

from agent_orchestrator.contracts import Task, TaskStatus
from agent_orchestrator.contracts.models import Budget, ClaimProposal, ResultEnvelope
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_PROFILE
from agent_orchestrator.verification.deterministic_checks import check_arbitration


def _task(criteria, directory="arbitration/k"):
    return Task(
        id="task-1",
        mission_id="mission-1",
        parent_task_ids=(),
        dependency_ids=(),
        goal="仲裁 k",
        rationale="双方结论相反",
        success_criteria=tuple(criteria),
        verification_policy=("format_check", "rule_check"),
        allowed_tools=(),
        budget=Budget(max_tokens=1000),
        priority=1.0,
        status=TaskStatus.READY,
        version=1,
        kind="conflict",
        context={"artifact_dir": directory},
    )


def _envelope(*, evidence, key="k", stance="affirms"):
    return ResultEnvelope(
        id="result-1",
        task_id="task-1",
        attempt_id="attempt-1",
        outcome="candidate",
        summary="裁决",
        artifacts=("arbitration/k/NOTE.md",),
        claims=(ClaimProposal(content="方案 A 成立", confidence=0.9, key=key, stance=stance),),
        evidence=tuple(evidence),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )


def test_p33_a22_the_code_domain_still_demands_a_probe_test() -> None:
    """A07 的钉子：code 领域的仲裁判据一个字不变。"""

    task = _task(["arbitration:k", "pytest:arbitration/k/test_probe.py"])
    assert check_arbitration(_envelope(evidence=["未跑检查"]), task, domain=CODE_PROFILE)
    assert not check_arbitration(
        _envelope(evidence=["pytest:arbitration/k/test_probe.py"]), task, domain=CODE_PROFILE
    )


def test_p33_a23_the_code_domain_still_demands_the_probe_run_in_its_own_directory() -> None:
    task = _task(["arbitration:k", "pytest:arbitration/k/test_probe.py"])
    problems = check_arbitration(
        _envelope(evidence=["pytest:tests/test_elsewhere.py"]), task, domain=CODE_PROFILE
    )
    assert problems and "arbitration/k" in problems[0]


def test_p33_a24_the_document_domain_does_not_demand_pytest_evidence() -> None:
    """文档领域的裁决层是 human_review，要求引一条它不允许出现的证据等于把任务判死。"""

    task = _task(["arbitration:k"])
    assert not check_arbitration(
        _envelope(evidence=["source:sources/a.md"]), task, domain=DOC_PROFILE
    )


def test_p33_a25_every_domain_still_demands_exactly_one_claim_on_the_key() -> None:
    """不投票这条对两个领域都不变：仲裁必须收敛到一条结论。"""

    task = _task(["arbitration:k"])
    empty = ResultEnvelope(
        id="result-2",
        task_id="task-1",
        attempt_id="attempt-1",
        outcome="candidate",
        summary="裁决",
        artifacts=("arbitration/k/NOTE.md",),
        claims=(ClaimProposal(content="别的主题", confidence=0.5, key="other"),),
        evidence=("source:sources/a.md",),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    problems = check_arbitration(empty, task, domain=DOC_PROFILE)
    assert problems and "exactly one claim" in problems[0]
