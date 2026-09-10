# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic, role-routed Provider for tests and the ``fixtures`` demo (D26).

Requests are routed on the ``[role:<name>]`` marker at the start of the system
message; each role has its own script queue.  A step is a string (final assistant
text), ``(tool_name, arguments)`` (a tool call) or a callable ``(request) -> step``
that may read the task package (to echo the real ``attempt_id`` into the Result
Envelope).  ``UnknownAfterHandoff`` raises after the SDK handed the request off,
which the SDK settles as an UNKNOWN invocation (S2-08).  This module is shipped in
the wheel so ``python -m agent_orchestrator demo --provider fixtures`` works in a
clean install (ORCH §14.3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from simple_harness import Message, MessageRole
from simple_harness.contracts import CallId
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderUsage,
)

MODEL = "agent-model"


class UnknownAfterHandoff(RuntimeError):
    """Raised by a script step to leave the provider invocation UNKNOWN."""


def role_of(request: ProviderRequest) -> str:
    for message in request.messages:
        content = message.content if isinstance(message.content, str) else str(message.content)
        match = re.match(r"\[role:([a-z_]+)\]", content.strip())
        if match:
            return match.group(1)
    return "unknown"


def package_of(request: ProviderRequest) -> dict[str, Any]:
    """Recover the JSON sections of the task package from the last user message."""

    text = ""
    for message in reversed(request.messages):
        if str(message.role) == str(MessageRole.USER):
            text = message.content if isinstance(message.content, str) else str(message.content)
            break
    sections: dict[str, Any] = {}
    for header, body in re.findall(r"## ([a-z_]+)\n(.*?)(?=\n## |\Z)", text, re.DOTALL):
        body = body.strip()
        try:
            sections[header] = json.loads(body)
        except json.JSONDecodeError:
            sections[header] = body
    return sections


class RoleScriptedProvider:
    def __init__(self, scripts: dict[str, Sequence[object]], *, usage_tokens: int = 100) -> None:
        self.scripts = {role: list(steps) for role, steps in scripts.items()}
        self.requests: list[ProviderRequest] = []
        self.by_role: dict[str, int] = {}
        self.usage_tokens = usage_tokens

    @property
    def calls(self) -> int:
        return len(self.requests)

    def extend(self, role: str, steps: Sequence[object]) -> None:
        self.scripts.setdefault(role, []).extend(steps)

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:  # type: ignore[no-untyped-def]
        del cancel
        self.requests.append(request)
        role = role_of(request)
        self.by_role[role] = self.by_role.get(role, 0) + 1
        queue = self.scripts.get(role)
        if not queue:
            raise AssertionError(f"scripted provider exhausted for role {role!r}")
        step = queue.pop(0)
        if callable(step) and not isinstance(step, (str, tuple)):
            step = step(request)
        if isinstance(step, UnknownAfterHandoff) or step is UnknownAfterHandoff:
            raise UnknownAfterHandoff("scripted transport loss after handoff")
        usage = ProviderUsage(
            input_tokens=self.usage_tokens,
            output_tokens=self.usage_tokens // 2,
            total_tokens=self.usage_tokens + self.usage_tokens // 2,
        )
        if isinstance(step, str):
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, step),
                model=MODEL,
                finish_reason="stop",
                usage=usage,
            )
        if not isinstance(step, tuple) or len(step) != 2:
            raise AssertionError(f"bad script step for role {role!r}: {step!r}")
        name, arguments = step
        call = ProviderToolCall(CallId(f"call-{len(self.requests)}"), str(name), dict(arguments))
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, ""),
            tool_calls=(call,),
            model=MODEL,
            finish_reason="tool_calls",
            usage=usage,
        )


def envelope_step(
    *,
    summary: str,
    artifacts: Sequence[str],
    claims: Sequence[str],
    outcome: str = "candidate",
    override: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Callable[[ProviderRequest], str]:
    """A script step that emits a Result Envelope bound to the real attempt identity."""

    def step(request: ProviderRequest) -> str:
        package = package_of(request)
        contract = package.get("task_contract", {})
        attempt = package.get("attempt", {})
        envelope = {
            "task_id": contract.get("task_id", ""),
            "attempt_id": attempt.get("attempt_id", ""),
            "outcome": outcome,
            "summary": summary,
            "claims": [{"content": claim, "confidence": 0.8} for claim in claims],
            "evidence": list(artifacts),
            "artifacts": list(artifacts),
            "proposed_tasks": [],
            "used_knowledge": [],
            "risks": [],
            "cost": {"tool_calls": 0},
        }
        if override is not None:
            envelope = override(envelope)
        return "<result_envelope>" + json.dumps(envelope, ensure_ascii=False) + "</result_envelope>"

    return step


def proposal_step(proposal: dict[str, Any]) -> str:
    return "<task_proposal>" + json.dumps(proposal, ensure_ascii=False) + "</task_proposal>"


def critic_step(
    *, verdict: str, criteria_met: bool, blocker: str | None = None
) -> Callable[[ProviderRequest], str]:
    def step(request: ProviderRequest) -> str:
        package = package_of(request)
        criteria = package.get("mission_success_criteria", [])
        findings = [] if blocker is None else [{"severity": "blocker", "detail": blocker}]
        body = {
            "verdict": verdict,
            "findings": findings,
            "mission_criteria": [
                {"criterion": c, "met": criteria_met, "reason": "scripted"} for c in criteria
            ],
        }
        return "<critic_verdict>" + json.dumps(body, ensure_ascii=False) + "</critic_verdict>"

    return step


# ---------------------------------------------------------------- demo scenario
DEMO_SEED = {
    "parse_kv.py": "def parse_kv(text):\n    raise NotImplementedError\n",
    "tests/test_parse_kv.py": (
        "from parse_kv import parse_kv\n\n\n"
        "def test_basic():\n    assert parse_kv('a=1;b=2') == {'a': '1', 'b': '2'}\n\n\n"
        "def test_empty():\n    assert parse_kv('') == {}\n"
    ),
}
DEMO_GOOD = "def parse_kv(text):\n    return dict(p.split('=', 1) for p in text.split(';') if p)\n"
DEMO_BAD = "def parse_kv(text):\n    return dict(p.split('=', 1) for p in text.split(';'))\n"
DEMO_PROPOSAL = {
    "goal": "实现 parse_kv(text) -> dict 并通过 tests/test_parse_kv.py",
    "rationale": "Mission 只有这一件工作；通过给定测试即满足成功条件",
    "success_criteria": ["pytest:tests/test_parse_kv.py", "file:parse_kv.py"],
    "verification_policy": ["format_check", "rule_check", "critic_review", "code_test"],
    "allowed_tools": ["workspace_read_file", "workspace_write_file", "workspace_list", "run_tests"],
    "budget": {"max_tokens": 50000, "max_attempts": 2},
    "priority": 1.0,
    "root_goal": "实现字符串解析函数并通过测试",
}


def demo_worker_script(code: str) -> list[object]:
    return [
        ("workspace_list", {}),
        ("workspace_read_file", {"path": "tests/test_parse_kv.py"}),
        ("workspace_write_file", {"path": "parse_kv.py", "content": code}),
        ("run_tests", {"path": "tests/test_parse_kv.py"}),
        envelope_step(
            summary="实现了 parse_kv",
            artifacts=["parse_kv.py"],
            claims=["parse_kv 通过 tests/test_parse_kv.py"],
        ),
    ]


def demo_single_task_provider() -> RoleScriptedProvider:
    """S2-01 + S2-02 in one run: a wrong first submission, then the repair passes."""

    return RoleScriptedProvider(
        {
            "planner": [proposal_step(DEMO_PROPOSAL)],
            "worker": demo_worker_script(DEMO_BAD) + demo_worker_script(DEMO_GOOD),
            "critic": [
                critic_step(verdict="PASS", criteria_met=True),
                critic_step(verdict="PASS", criteria_met=True),
            ],
        }
    )


__all__ = (
    "DEMO_BAD",
    "DEMO_GOOD",
    "DEMO_PROPOSAL",
    "DEMO_SEED",
    "MODEL",
    "RoleScriptedProvider",
    "UnknownAfterHandoff",
    "critic_step",
    "demo_single_task_provider",
    "demo_worker_script",
    "envelope_step",
    "package_of",
    "proposal_step",
    "role_of",
)
