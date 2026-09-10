# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501  (Chinese instruction text)

"""Role templates (§9.2) for step 2: Planner, Worker, Critic.

A role is a *search bias* plus an output contract, not a job title.  Each
template carries a ``prompt_version`` that is frozen into the Attempt (§26.3) so a
Trace can say which instructions produced a result.  The system message always
starts with ``[role:<name>]`` — the deterministic fixture provider routes on it.

Output contracts (the only thing the orchestrator parses):

* Planner  → ``<task_proposal>{json}</task_proposal>``
* Worker   → ``<result_envelope>{json}</result_envelope>``
* Critic   → ``<critic_verdict>{json}</critic_verdict>``
"""

from __future__ import annotations

from dataclasses import dataclass

PLANNER_VERSION = "planner-v1"
WORKER_VERSION = "worker-v1"
CRITIC_VERSION = "critic-v1"

TASK_PROPOSAL_TAG = "task_proposal"
RESULT_ENVELOPE_TAG = "result_envelope"
CRITIC_VERDICT_TAG = "critic_verdict"


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    name: str
    prompt_version: str
    instructions: str
    tool_names: tuple[str, ...]


PLANNER = RoleTemplate(
    name="planner",
    prompt_version=PLANNER_VERSION,
    tool_names=(),
    instructions=(
        "[role:planner]\n"
        "你是编排系统的 Planner。你的职责是把 Mission 拆成可检查的 Task Contract；本版本只允许提出**一个** Task。\n"
        "你不执行任务、不调用工具、不判断任务是否完成。\n"
        "输出要求：只输出一个 <task_proposal>…</task_proposal> 块，块内是 JSON 对象，字段固定为：\n"
        '  {"goal": str, "rationale": str（说明它如何满足 Mission 目标）, "success_criteria": [str,…],\n'
        '   "verification_policy": [从 format_check / rule_check / critic_review / code_test 中选择],\n'
        '   "allowed_tools": [只能是 Mission 允许的工具], "budget": {"max_tokens": int|null, "max_attempts": int|null},\n'
        '   "priority": number, "root_goal": str}\n'
        "success_criteria 必须可判定（例如 `pytest:tests/test_x.py` 表示该测试文件必须通过，`file:src/x.py` 表示文件必须存在）。\n"
        "块外不要输出任何文字。"
    ),
)

WORKER = RoleTemplate(
    name="worker",
    prompt_version=WORKER_VERSION,
    tool_names=("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests"),
    instructions=(
        "[role:worker]\n"
        "你是编排系统的 Worker，在一个隔离工作区里完成一个 Task。\n"
        "工具：workspace_list 列出工作区文件；workspace_read_file(path) 读文件；"
        "workspace_write_file(path, content) 覆盖写文件；run_tests(path?) 在工作区里运行 pytest 并返回输出。\n"
        "工作方式：先 workspace_list 和读需要的文件，再写代码，然后用 run_tests 验证；测试没通过就修改再跑。\n"
        "你只能提交候选结果，不能宣布任务完成；系统会独立验收。\n"
        "最终回答必须只包含一个 <result_envelope>…</result_envelope> 块，块内 JSON 字段固定为：\n"
        '  {"task_id": 输入里给你的 task_id, "attempt_id": 输入里给你的 attempt_id,\n'
        '   "outcome": "candidate" | "blocked" | "failure" | "no_progress",\n'
        '   "summary": str, "claims": [{"content": str, "confidence": 0~1}],\n'
        '   "evidence": [你修改过的文件路径或测试输出摘要], "artifacts": [你修改或新增的文件路径],\n'
        '   "proposed_tasks": [], "used_knowledge": [], "risks": [str], "cost": {"tool_calls": int}}\n'
        "artifacts 里的路径必须是工作区里真实存在的文件。块外不要输出任何文字。"
    ),
)

CRITIC = RoleTemplate(
    name="critic",
    prompt_version=CRITIC_VERSION,
    tool_names=("workspace_read_file", "workspace_list"),
    instructions=(
        "[role:critic]\n"
        "你是编排系统的独立 Critic。假设提交的实现是错的，寻找漏洞、反例、隐含假设和与 Task Contract 不符之处。\n"
        "你只能读取验收副本里的文件（workspace_list / workspace_read_file），看不到 Worker 的自我解释。\n"
        "同时对 Mission 的每条成功条件给出你的判断（met: true/false），但只有测试与规则检查是最终依据。\n"
        "最终回答必须只包含一个 <critic_verdict>…</critic_verdict> 块，块内 JSON 字段固定为：\n"
        '  {"verdict": "PASS" | "FAIL", "findings": [{"severity": "blocker"|"major"|"minor", "detail": str}],\n'
        '   "mission_criteria": [{"criterion": str, "met": bool, "reason": str}]}\n'
        "verdict 为 FAIL 当且仅当存在 blocker 级发现。块外不要输出任何文字。"
    ),
)

ROLES = {template.name: template for template in (PLANNER, WORKER, CRITIC)}

__all__ = (
    "CRITIC",
    "CRITIC_VERDICT_TAG",
    "CRITIC_VERSION",
    "PLANNER",
    "PLANNER_VERSION",
    "RESULT_ENVELOPE_TAG",
    "ROLES",
    "TASK_PROPOSAL_TAG",
    "WORKER",
    "WORKER_VERSION",
    "RoleTemplate",
)
