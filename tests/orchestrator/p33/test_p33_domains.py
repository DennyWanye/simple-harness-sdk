# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.3 切片 A · 领域画像与五处闸门（plan v3 D1；验收 P33-09、P33-19 的基础）。

一份领域画像是**部署给出的常量**，Mission 创建时冻结。它不只给验证政策设下限，还要能
**替换**系统自己生成的任务模板——第 1 轮评审查出 `conflict_task` 的准则里写死了
`pytest:<dir>/test_probe.py`、`CONFLICT_POLICY` 与 synthesis 默认政策含 `code_test`，
所以一个禁用 pytest 的领域如果只设下限，系统模板会被自己的闸门拒掉，或者建出来永远跑不完。

`code-v1` 必须与本轮改动前**逐字一致**（A07）。
"""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import SYSTEM_DEFAULT_POLICY
from agent_orchestrator.governance.domains import (
    CODE_DOMAIN,
    DOC_DOMAIN,
    LEGACY_DOMAIN,
    DomainProfileV1,
    check_against_domain,
    resolve_domain,
)
from agent_orchestrator.planning.manager import CONFLICT_POLICY


# ---------------------------------------------------------------- 注册表与冻结


def test_p33_a01_two_domains_are_registered_and_resolvable() -> None:
    assert resolve_domain(CODE_DOMAIN).id == CODE_DOMAIN
    assert resolve_domain(DOC_DOMAIN).id == DOC_DOMAIN
    assert isinstance(resolve_domain(DOC_DOMAIN), DomainProfileV1)


def test_p33_a02_an_unknown_domain_is_refused_not_defaulted() -> None:
    with pytest.raises(KeyError):
        resolve_domain("whatever-v9")


def test_p33_a03_missions_from_before_this_version_bind_the_code_domain() -> None:
    """0.10 之前的 Mission 没有领域绑定；它们必须落回与今天完全相同的行为。"""

    assert LEGACY_DOMAIN == CODE_DOMAIN


def test_p33_a04_the_code_domain_repeats_todays_behaviour_verbatim() -> None:
    """A07：旧政策语义不变。这条是钉子——改了 code-v1 就会红。"""

    code = resolve_domain(CODE_DOMAIN)
    assert code.default_policy == SYSTEM_DEFAULT_POLICY
    assert code.conflict_template.policy == CONFLICT_POLICY
    assert code.conflict_template.decides_with == "code_test"
    assert code.external_check == "code_test"
    assert "pytest" in code.allowed_evidence_kinds
    assert "tool-run" in code.allowed_evidence_kinds
    assert "knowledge" in code.allowed_evidence_kinds


def test_p33_a05_the_document_domain_has_no_pytest_anywhere() -> None:
    doc = resolve_domain(DOC_DOMAIN)
    assert "pytest" not in doc.allowed_evidence_kinds
    assert "pytest" not in doc.criterion_kinds
    # 第 1 轮评审 B P0-4：`tool-run:` 现在不可核验（call_key = run_id:call_id，
    # 模型写信封时两个 id 都不知道），文档领域直接不允许它出现
    assert "tool-run" not in doc.allowed_evidence_kinds
    assert "source" in doc.allowed_evidence_kinds
    # 系统模板必须被替换，而不只是设下限
    assert "code_test" not in doc.default_policy
    assert "code_test" not in doc.conflict_template.policy
    assert "code_test" not in doc.synthesis_default_policy
    # 第 2 轮评审 A P1-E：文档领域没有可确定判定的外部检查，仲裁走人工
    assert doc.conflict_template.decides_with == "human_review"
    assert "human_review" in doc.conflict_template.policy


def test_p33_a06_the_document_conflict_template_asks_for_no_probe_test() -> None:
    doc = resolve_domain(DOC_DOMAIN)
    criteria = doc.conflict_template.criteria_for(key="k", directory="arbitration/k")
    assert any(c.startswith("arbitration:") for c in criteria)
    assert not any(c.startswith("pytest:") for c in criteria)


# ---------------------------------------------------------------- 闸门本身


def _problems(domain_id: str, **kwargs) -> list[str]:
    return check_against_domain(resolve_domain(domain_id), key="T1", **kwargs)


def test_p33_a07_a_pytest_criterion_is_refused_in_the_document_domain() -> None:
    problems = _problems(
        DOC_DOMAIN,
        success_criteria=("pytest:tests/test_x.py",),
        verification_policy=("format_check", "rule_check"),
    )
    assert problems
    assert any("pytest" in p for p in problems)


def test_p33_a08_a_pytest_criterion_is_fine_in_the_code_domain() -> None:
    assert not _problems(
        CODE_DOMAIN,
        success_criteria=("pytest:tests/test_x.py",),
        verification_policy=SYSTEM_DEFAULT_POLICY,
    )


def test_p33_a09_a_policy_below_the_floor_is_refused() -> None:
    """只有 format_check 的政策等于没有实质检查。"""

    problems = _problems(
        DOC_DOMAIN, success_criteria=("file:REPORT.md",), verification_policy=("format_check",)
    )
    assert problems


def test_p33_a10_a_policy_naming_a_layer_the_domain_replaced_is_refused() -> None:
    """文档领域不跑 code_test；一个要求它的任务在闸门就被拒，而不是建出来永远 FAIL。"""

    problems = _problems(
        DOC_DOMAIN,
        success_criteria=("file:REPORT.md",),
        verification_policy=("format_check", "rule_check", "code_test"),
    )
    assert any("code_test" in p for p in problems)


def test_p33_a11_an_evidence_kind_outside_the_domain_is_refused() -> None:
    problems = _problems(
        DOC_DOMAIN,
        success_criteria=("file:REPORT.md",),
        verification_policy=("format_check", "rule_check"),
        evidence_kinds=("tool-run",),
    )
    assert any("tool-run" in p for p in problems)


def test_p33_a12_the_document_domain_accepts_its_own_shape() -> None:
    assert not _problems(
        DOC_DOMAIN,
        success_criteria=("file:REPORT.md", "cite:conclusion-1"),
        verification_policy=("format_check", "rule_check", "critic_review"),
        evidence_kinds=("source", "artifact"),
    )


def test_p33_a13_the_code_domain_still_accepts_every_layer_name() -> None:
    """A07 的钉子：本轮不得让 code-v1 开始拒绝它以前接受的政策。

    `formal_check` 今天由部署的 `deployed_layers` 过滤（未部署就在既有闸门被拒），
    到不了这里；领域闸门不能替它多拒一次，否则旧 Mission 的语义就变了。
    """

    assert not _problems(
        CODE_DOMAIN,
        success_criteria=("pytest:tests/test_x.py",),
        verification_policy=tuple(resolve_domain(CODE_DOMAIN).runs_layers),
    )
