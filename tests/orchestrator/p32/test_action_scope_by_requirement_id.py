# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""分层步骤的要求是编号（c-user-5 = 第 5 条原始要求），按编号收窄操作说明（旧式显式声明
actions/*.json 的任务仍用；完成协议下申请单由系统生成，见 test_system_operations）。
"""

from __future__ import annotations

from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.runtime.action_schema import (
    OPERATION_CANDIDATE_FILE,
    worker_action_contract,
)
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector

MISSION = [
    "README.md 引用 top_words",
    "action:file_publish.publish:README.md",
    "action:file_publish.publish:wordfreq.py",
]
ENABLED = DeploymentPolicy(enabled_connectors=("file_publish",))


def _contract(tmp_path, task_criteria):
    return worker_action_contract(
        mission_criteria=MISSION, task_criteria=task_criteria,
        task_outputs=[OPERATION_CANDIDATE_FILE],
        connectors={"file_publish": FilePublishConnector(tmp_path / "pub", tmp_path / "ledger")},
        deployment=ENABLED,
    )



def test_a_step_sees_only_its_own_operation_by_requirement_number(tmp_path):
    contract = _contract(tmp_path, ["c-user-1", "c-user-3"])
    assert [op["target"] for op in contract["operations"]] == ["wordfreq.py"]
    both = _contract(tmp_path, ["c-user-2", "c-user-3"])
    assert len(both["operations"]) == 2


def test_a_numbered_step_that_owns_no_operation_is_told_nothing_about_candidates(tmp_path):
    """2026-09-28 真机第五局：写文件那一步（只负责内容要求）被退回"整个任务的两个发布"，
    屡次写申请单被规则检查退回，5 次尝试白费。"""
    assert _contract(tmp_path, ["c-user-1", "pytest: test_wordfreq.py"]) is None
    # 不带编号的旧式步骤：照旧退回整个任务的操作
    legacy = _contract(tmp_path, ["README.md 引用 top_words"])
    assert len(legacy["operations"]) == 2
