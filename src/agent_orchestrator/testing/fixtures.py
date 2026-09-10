# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501  (scripted demo content)

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

import asyncio
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
    def __init__(
        self,
        scripts: dict[str, Sequence[object]],
        *,
        usage_tokens: int = 100,
        model: str = MODEL,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.scripts = {role: list(steps) for role, steps in scripts.items()}
        self.requests: list[ProviderRequest] = []
        self.by_role: dict[str, int] = {}
        self.usage_tokens = usage_tokens
        self.model = model
        self.gate = gate  # when set, every call waits here (a stalled executor)

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
        if self.gate is not None:
            await self.gate.wait()
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
                model=self.model,
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
            model=self.model,
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


def graph_proposal_step(tasks: Sequence[dict[str, Any]]) -> str:
    body = json.dumps({"tasks": list(tasks)}, ensure_ascii=False)
    return "<task_graph_proposal>" + body + "</task_graph_proposal>"


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
    "DEMO_DAG_SPEC",
    "DEMO_DAG_TASKS",
    "TEXTKIT_SEED",
    "TaskRoutedProvider",
    "critic_step",
    "demo_single_task_provider",
    "demo_static_dag_provider",
    "demo_static_dag_scripts",
    "demo_worker_script",
    "envelope_step",
    "graph_proposal_step",
    "package_of",
    "proposal_step",
    "role_of",
)


# ------------------------------------------------------------ static-dag demo
TEXTKIT_SEED = {
    "README.md": "# textkit\n\n一个小工具包：slugify 与 word_count。\n",
    "tests/test_slug.py": (
        "from textkit.slug import slugify\n\n\n"
        "def test_slugify_basic():\n    assert slugify('Hello, World!') == 'hello-world'\n\n\n"
        "def test_slugify_collapses():\n    assert slugify('  a   b  ') == 'a-b'\n"
    ),
    "tests/test_count.py": (
        "from textkit.count import word_count\n\n\n"
        "def test_word_count_basic():\n    assert word_count('a b  c') == 3\n\n\n"
        "def test_word_count_empty():\n    assert word_count('') == 0\n"
    ),
    "tests/test_integration.py": (
        "from textkit import slugify, word_count\n\n\n"
        "def test_exports():\n    text = 'Hello big World'\n"
        "    assert slugify(text) == 'hello-big-world' and word_count(text) == 3\n"
    ),
}
TEXTKIT_CONTRACT = (
    '"""textkit 公共合同：两个纯函数。"""\n\n'
    "from textkit.count import word_count\n"
    "from textkit.slug import slugify\n\n"
    '__all__ = ("slugify", "word_count")\n'
)
TEXTKIT_SLUG_STUB = "def slugify(text: str) -> str:\n    raise NotImplementedError\n"
TEXTKIT_COUNT_STUB = "def word_count(text: str) -> int:\n    raise NotImplementedError\n"
TEXTKIT_SLUG = (
    "import re\n\n\n"
    "def slugify(text: str) -> str:\n"
    "    words = re.findall(r'[A-Za-z0-9]+', text.lower())\n"
    "    return '-'.join(words)\n"
)
TEXTKIT_COUNT = "def word_count(text: str) -> int:\n    return len(text.split())\n"
TEXTKIT_DELIVERY = (
    "# 交付说明\n\n- `textkit.slugify(text)`：小写、非字母数字换成 `-`、折叠空白。\n"
    "- `textkit.word_count(text)`：按空白分词计数，空串为 0。\n- 测试：`pytest tests`。\n"
)


def _task(key, goal, deps, criteria, tokens, priority=1.0, policy=None, outputs=()):
    return {
        "key": key,
        "goal": goal,
        "rationale": f"{key} 是 A→(B‖C)→D→E 计划的一部分，服务 Mission 目标",
        "dependencies": list(deps),
        "success_criteria": list(criteria),
        "verification_policy": policy or ["format_check", "rule_check", "code_test"],
        "outputs": list(outputs),
        "allowed_tools": [
            "workspace_read_file",
            "workspace_write_file",
            "workspace_list",
            "run_tests",
        ],
        "budget": {"max_tokens": tokens, "max_attempts": 2},
        "priority": priority,
    }


DEMO_DAG_TASKS = [
    _task(
        "A",
        "写出 textkit 包的公共合同（textkit/__init__.py 导出 slugify 与 word_count）与两个函数的桩",
        (),
        ["file:textkit/__init__.py", "file:textkit/slug.py", "file:textkit/count.py"],
        20_000,
        3.0,
        policy=["format_check", "rule_check"],  # stubs only: nothing to test yet
        outputs=["textkit/__init__.py", "textkit/slug.py", "textkit/count.py"],
    ),
    _task(
        "B",
        "实现 textkit/slug.py 的 slugify 并通过 tests/test_slug.py",
        ["A"],
        ["pytest:tests/test_slug.py"],
        30_000,
        2.0,
        outputs=["textkit/slug.py"],
    ),
    _task(
        "C",
        "实现 textkit/count.py 的 word_count 并通过 tests/test_count.py",
        ["A"],
        ["pytest:tests/test_count.py"],
        30_000,
        2.0,
        outputs=["textkit/count.py"],
    ),
    _task(
        "D",
        "集成测试：tests/test_integration.py 通过",
        ["B", "C"],
        ["pytest:tests/test_integration.py"],
        20_000,
        1.0,
    ),
    _task(
        "E",
        "写 DELIVERY.md 交付说明并确保全部测试通过",
        ["D"],
        ["file:DELIVERY.md", "pytest:tests"],
        20_000,
        1.0,
        outputs=["DELIVERY.md"],
    ),
]


def _write_then_envelope(
    files: dict[str, str], *, test_path: str | None, summary: str, artifacts: list[str], claim: str
) -> list[object]:
    steps: list[object] = [("workspace_list", {})]
    for path, content in files.items():
        steps.append(("workspace_write_file", {"path": path, "content": content}))
    if test_path is not None:
        steps.append(("run_tests", {"path": test_path}))
    steps.append(envelope_step(summary=summary, artifacts=artifacts, claims=[claim]))
    return steps


def demo_static_dag_scripts(*, c_first_wrong: bool = False) -> dict[str, list[object]]:
    """Worker scripts for A→(B‖C)→D→E; workers are routed by role, so the scripts are
    consumed in dispatch order (A, then B and C in parallel, D, E).  ``c_first_wrong``
    makes C's first submission fail its tests (S3-03)."""

    a = _write_then_envelope(
        {
            "textkit/__init__.py": TEXTKIT_CONTRACT,
            "textkit/slug.py": TEXTKIT_SLUG_STUB,
            "textkit/count.py": TEXTKIT_COUNT_STUB,
        },
        test_path=None,
        summary="写出了合同与桩",
        artifacts=["textkit/__init__.py", "textkit/slug.py", "textkit/count.py"],
        claim="合同文件存在",
    )
    b = _write_then_envelope(
        {"textkit/slug.py": TEXTKIT_SLUG},
        test_path="tests/test_slug.py",
        summary="实现 slugify",
        artifacts=["textkit/slug.py"],
        claim="tests/test_slug.py 通过",
    )
    c_ok = _write_then_envelope(
        {"textkit/count.py": TEXTKIT_COUNT},
        test_path="tests/test_count.py",
        summary="实现 word_count",
        artifacts=["textkit/count.py"],
        claim="tests/test_count.py 通过",
    )
    c_bad = _write_then_envelope(
        {
            "textkit/count.py": "def word_count(text: str) -> int:\n    return len(text.split()) + 1\n"
        },
        test_path="tests/test_count.py",
        summary="实现 word_count",
        artifacts=["textkit/count.py"],
        claim="tests/test_count.py 通过",
    )
    d = _write_then_envelope(
        {},
        test_path="tests/test_integration.py",
        summary="集成测试通过",
        artifacts=["textkit/__init__.py"],
        claim="tests/test_integration.py 通过",
    )
    e = _write_then_envelope(
        {"DELIVERY.md": TEXTKIT_DELIVERY},
        test_path="tests",
        summary="交付说明完成",
        artifacts=["DELIVERY.md"],
        claim="全部测试通过",
    )
    return {"A": a, "B": b, "C": (c_bad if c_first_wrong else []) + c_ok, "D": d, "E": e}


class TaskRoutedProvider(RoleScriptedProvider):
    """Worker requests are routed by the Task's graph key (read from the task package's
    goal), so parallel Workers each consume their own script (S3-01).

    ``holds`` maps a key to the events its successive *Attempts* wait on before their
    first model call (``None`` = no wait) — a held executor is how the tests observe parallelism, candidates and
    lease takeover.  ``calls_by_key`` counts calls that *reached* the script (a call
    cancelled while held is not a run).
    """

    def __init__(
        self,
        planner_steps,
        worker_by_key: dict[str, list[object]],
        critic_steps=(),
        *,
        goals: dict[str, str] | None = None,
        holds: dict[str, list[asyncio.Event | None]] | None = None,
        **kwargs,
    ) -> None:  # type: ignore[no-untyped-def]
        super().__init__({"planner": list(planner_steps), "critic": list(critic_steps)}, **kwargs)
        self.worker_by_key = {key: list(steps) for key, steps in worker_by_key.items()}
        self.goals = dict(DEMO_TASK_GOALS if goals is None else goals)
        self.holds = {key: list(events) for key, events in (holds or {}).items()}
        self.calls_by_key: dict[str, int] = {}
        self.seen_attempts: set[str] = set()
        self.inflight: set[str] = set()
        self.max_inflight = 0

    def _worker_key(self, request: ProviderRequest) -> str:
        goal = str(package_of(request).get("task_contract", {}).get("goal", ""))
        for key, task_goal in self.goals.items():
            if goal == task_goal:
                return key
        raise AssertionError(f"no worker script for goal {goal!r}")

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:  # type: ignore[no-untyped-def]
        if role_of(request) != "worker":
            return await super().invoke(request, cancel=cancel)
        key = self._worker_key(request)
        attempt_id = str(package_of(request).get("attempt", {}).get("attempt_id", key))
        hold = None
        if attempt_id not in self.seen_attempts:  # one hold per Attempt, on its first call
            self.seen_attempts.add(attempt_id)
            pending = self.holds.get(key)
            hold = pending.pop(0) if pending else None
        self.inflight.add(attempt_id)
        self.max_inflight = max(self.max_inflight, len(self.inflight))
        try:
            if hold is not None:
                await hold.wait()
            self.calls_by_key[key] = self.calls_by_key.get(key, 0) + 1
            queue = self.worker_by_key.get(key)
            if not queue:
                raise AssertionError(f"worker script exhausted for task {key!r}")
            self.scripts["worker"] = queue  # borrow the role queue for this call
            return await super().invoke(request, cancel=cancel)
        finally:
            self.inflight.discard(attempt_id)


DEMO_TASK_GOALS = {task["key"]: task["goal"] for task in DEMO_DAG_TASKS}


def demo_static_dag_provider(
    *,
    c_first_wrong: bool = False,
    planner_steps: Sequence[object] | None = None,
    holds: dict[str, list[asyncio.Event | None]] | None = None,
    extra_scripts: dict[str, list[object]] | None = None,
) -> TaskRoutedProvider:
    scripts = demo_static_dag_scripts(c_first_wrong=c_first_wrong)
    for key, steps in (extra_scripts or {}).items():
        scripts[key] = scripts.get(key, []) + list(steps)
    return TaskRoutedProvider(
        list(planner_steps) if planner_steps is not None else [graph_proposal_step(DEMO_DAG_TASKS)],
        scripts,
        critic_steps=[critic_step(verdict="PASS", criteria_met=True)] * 3,
        holds=holds,
    )


DEMO_DAG_SPEC = {
    "goal": "交付 textkit 小包：slugify 与 word_count 两个函数、集成测试与交付说明",
    "success_criteria": ["pytest:tests", "file:DELIVERY.md"],
    "allowed_tools": [
        "workspace_read_file",
        "workspace_write_file",
        "workspace_list",
        "run_tests",
    ],
    "budget": {"max_tokens": 200_000, "max_attempts": 12},
}
