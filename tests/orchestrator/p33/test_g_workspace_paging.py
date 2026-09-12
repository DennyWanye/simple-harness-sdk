# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Read paging oracle, written before implementation (real gateway, no Provider).

Pages must survive the real Agent context per-tool token threshold, not just a
content-length approximation: serialize the complete ToolResult and the actual
ReAct TOOL Message, then count with the production fallback and real BPE tokenizer.
Concatenation must recover every original UTF-8 byte (CRLF, combining characters,
emoji, table conditions and oversized lines). SHA binds continuation to those
bytes, never a normalized source version. Every page repeats permission/accounting.
Old small path-only reads retain their response shape; explicit paging is strict.
"""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256

import pytest
from fixtures_provider import MODEL, RoleScriptedProvider

from agent_orchestrator.artifacts.workspace import MAX_FILE_BYTES, WorkspaceManager
from agent_orchestrator.runtime.tool_gateway import (
    CRITIC_TOOLS,
    TOOL_SCHEMAS,
    UNTRUSTED_NOTICE,
    WorkspaceBinding,
    WorkspaceToolGateway,
)
from simple_harness.agents import AgentConfig, AgentTurnState, build_agent_runtime
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import (
    TiktokenTokenizer,
    UpperBoundTokenizer,
    count_message,
)
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import CallId, Message, MessageRole, canonical_json, thaw_json
from simple_harness.tools import ToolCall, ToolResult

PATH = "sources/条件.md"


def _gateway(tmp_path, data, **binding_options):
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create("attempt", seed={})
    target = workspace.resolve(PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    gateway = WorkspaceToolGateway(manager)
    binding = WorkspaceBinding(
        "attempt",
        "work",
        True,
        CRITIC_TOOLS,
        untrusted_sources=("sources/",),
        **binding_options,
    )
    gateway.bind("agent", binding)
    return gateway, workspace, binding


async def _read(gateway, *, agent="agent", **arguments):
    call = ToolCall(
        CallId(f"read-{len(gateway.calls)}"), "workspace_read_file", {"path": PATH, **arguments}
    )
    return await gateway.execute(call, {"run_id": agent})


def _wire(result):
    # Full persisted ToolResult shape, including fields not present in the TOOL
    # message. This is the public 3000-byte response bound, including trust notice.
    return canonical_json(
        {
            "call_id": result.call_id.value,
            "outcome": result.outcome.value,
            "value": thaw_json(result.value),
            "error_code": result.error_code,
            "public_message": result.public_message,
            "retryable": result.retryable,
        }
    )


def _message(result):
    # Match ReAct's real TOOL message; never count only result.value['content'].
    return Message(
        MessageRole.TOOL,
        canonical_json(
            {
                "outcome": result.outcome.value,
                "value": thaw_json(result.value),
                "error_code": result.error_code,
                "public_message": result.public_message,
            }
        ),
        name="workspace_read_file",
        call_id=result.call_id,
    )


TABLE = (
    "# 方案条件\r\n"
    + "背景资料，完整条件在下表，不能根据预览作结论。\r\n" * 45
    + "| 场景 | 条件与限制 |\r\n| --- | --- |\r\n"
    + "".join(f"| 场景{i} | 仅隔离环境可用，禁止对外发布😀 |\r\n" for i in range(60))
    + "最终条件：方案并非无条件支持离线。\r\n"
)


@pytest.mark.parametrize(
    "text",
    [
        TABLE,
        "罕见字𠀀与emoji😀🧬以及组合e\u0301" * 500,
        "无换行的完整长行" * 1500,
        '控制字符\t"\\\x00' * 650,
    ],
    ids=["full-table", "unicode", "long-single-line", "json-escaping"],
)
def test_pages_reassemble_raw_source_and_survive_actual_context_threshold(tmp_path, text):
    data = text.encode("utf-8")
    gateway, _, _ = _gateway(tmp_path, data)
    counters = (UpperBoundTokenizer(), TiktokenTokenizer())
    # The old whole-result route really exceeds the preview threshold.
    original = ToolResult.succeeded(CallId("old"), {"path": PATH, "content": text})
    assert count_message(counters[0], _message(original)) > ContextPolicy().max_tool_result_tokens

    async def run():
        pages = []
        offset = 0
        while True:
            args = (
                {}
                if offset == 0
                else {"offset": offset, "expected_sha256": sha256(data).hexdigest()}
            )
            result = await _read(gateway, **args)
            assert result.outcome.value == "succeeded", result
            page = result.value
            assert page["page_schema"] == "workspace-read-v1"
            assert page["offset"] == offset and page["total_chars"] == len(text)
            assert page["sha256"] == sha256(data).hexdigest()
            assert page["trust"] == "untrusted_external" and page["notice"] == UNTRUSTED_NOTICE
            assert len(_wire(result).encode("utf-8")) <= 2000
            for tokenizer in counters:
                assert (
                    count_message(tokenizer, _message(result))
                    <= ContextPolicy().max_tool_result_tokens
                )
            assert "value_preview" not in _message(result).content
            chunk = page["content"]
            assert chunk and chunk == text[offset : offset + len(chunk)]
            pages.append(chunk)
            offset += len(chunk)
            if page["next_offset"] is None:
                assert offset == len(text)
                break
            assert page["next_offset"] == offset
        assert len(pages) > 1
        joined = "".join(pages).encode("utf-8")
        assert joined == data and sha256(joined).hexdigest() == sha256(data).hexdigest()
        assert gateway.executed_calls("agent") == len(pages)
        if text == TABLE:
            assert b"|" in joined
            assert "最终条件：方案并非无条件支持离线。" in joined.decode("utf-8")
            assert joined.count(b"\r\n") == data.count(b"\r\n")

    asyncio.run(run())


@pytest.mark.parametrize("untrusted", [False, True])
def test_old_small_result_shape_and_raw_crlf_are_preserved(tmp_path, untrusted):
    data = "甲e\u0301😀\r\n乙\r\n".encode("utf-8")
    gateway, _, binding = _gateway(tmp_path, data)
    if not untrusted:
        gateway.bind("agent", replace(binding, untrusted_sources=()))
    result = asyncio.run(_read(gateway))
    expected = {"path": PATH, "content": data.decode("utf-8")}
    if untrusted:
        expected.update(trust="untrusted_external", notice=UNTRUSTED_NOTICE)
    assert thaw_json(result.value) == expected


def test_explicit_codepoint_pages_report_original_line_boundaries(tmp_path):
    text = "甲😀\r\n乙。\n末"
    gateway, _, _ = _gateway(tmp_path, text.encode("utf-8"))
    expected_lines = [1, 1, 1, 1, 2, 2, 2, 3]
    starts_mid = [False, True, True, True, False, True, True, False]
    ends_mid = [True, True, True, False, True, True, False, False]

    async def run():
        for offset, char in enumerate(text):
            result = await _read(
                gateway,
                offset=offset,
                max_chars=1,
                expected_sha256=sha256(text.encode("utf-8")).hexdigest(),
            )
            assert result.outcome.value == "succeeded", result
            page = result.value
            assert page["content"] == char
            assert page["start_line"] == page["end_line"] == expected_lines[offset]
            assert page["starts_mid_line"] is starts_mid[offset]
            assert page["ends_mid_line"] is ends_mid[offset]
            assert page["next_offset"] == (offset + 1 if offset < len(text) - 1 else None)
        eof = await _read(
            gateway, offset=len(text), expected_sha256=sha256(text.encode()).hexdigest()
        )
        assert eof.value["content"] == "" and eof.value["next_offset"] is None
        assert eof.value["start_line"] == eof.value["end_line"] == 3
        assert not eof.value["starts_mid_line"] and not eof.value["ends_mid_line"]

    asyncio.run(run())


def test_line_numbers_do_not_treat_unicode_separators_as_source_newlines(tmp_path):
    text = "甲\u2028乙\u0085丙\v丁\r戊\n己"
    gateway, _, _ = _gateway(tmp_path, text.encode("utf-8"))
    digest = sha256(text.encode("utf-8")).hexdigest()

    async def run():
        for offset, line in [(2, 1), (4, 1), (6, 1), (8, 2), (10, 3)]:
            result = await _read(gateway, offset=offset, max_chars=1, expected_sha256=digest)
            assert result.value["start_line"] == result.value["end_line"] == line

    asyncio.run(run())


@pytest.mark.parametrize("tokenizer_kind", ["fallback", "tiktoken"])
def test_actual_agent_context_port_keeps_pages_visible_in_journal_and_provider(
    tmp_path, tokenizer_kind
):
    gateway, _, binding = _gateway(tmp_path, TABLE.encode("utf-8"))
    seen = []

    def received(request):
        message = next(m for m in reversed(request.messages) if m.role is MessageRole.TOOL)
        payload = json.loads(message.content)
        assert "value_preview" not in payload and not payload.get("truncated")
        page = payload["value"]
        assert page["content"] and page["notice"] == UNTRUSTED_NOTICE
        seen.append(page)
        if len(seen) == 1:
            return "workspace_read_file", {
                "path": PATH,
                "offset": page["next_offset"],
                "expected_sha256": page["sha256"],
            }
        assert page["offset"] == seen[0]["next_offset"]
        return "已完整收到两页工具结果"

    async def run():
        provider = RoleScriptedProvider(
            {
                "worker": [("workspace_read_file", {"path": PATH}), received, received],
            }
        )
        tokenizer = UpperBoundTokenizer() if tokenizer_kind == "fallback" else TiktokenTokenizer()
        ports = AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "agent.db"),
            model=MODEL,
            owner_id="paging",
            context_policy=ContextPolicy(),
            tokenizer=tokenizer,
            tool_executor=gateway,
            tool_names=("workspace_read_file",),
            tool_schemas={"workspace_read_file": TOOL_SCHEMAS["workspace_read_file"]},
        )
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(
                AgentConfig(
                    name="reader",
                    instructions="[role:worker]\n读取来源数据。",
                    model_profile_ref="p",
                    tool_names=("workspace_read_file",),
                ),
                creation_key="paging-reader",
            )
            gateway.bind(agent.agent_id, binding)
            result = await agent.ask("读两页", input_id="read-two", timeout=15)
            assert result.state is AgentTurnState.COMMITTED
            rows = [row for row in agent.journal() if row.kind == "tool_result"]
            assert len(rows) == len(seen) == 2
            assert all(row.visibility == "context" and row.full_record_seq is None for row in rows)
            assert all("value_preview" not in row.message_json["content"] for row in rows)
            assert provider.by_role == {"worker": 3}

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": True},
        {"offset": 1.0},
        {"offset": -1},
        {"offset": "1"},
        {"max_chars": False},
        {"max_chars": 1.5},
        {"max_chars": 0},
        {"max_chars": 4097},
        {"expected_sha256": "A" * 64},
        {"expected_sha256": "a" * 63},
        {"expected_sha256": True},
        {"offset": 1},
        {"lines": 1},
    ],
)
def test_paging_schema_rejects_invalid_types_ranges_and_missing_continuation_hash(
    tmp_path, arguments
):
    gateway, _, _ = _gateway(tmp_path, b"abc")
    result = asyncio.run(_read(gateway, **arguments))
    assert result.error_code == "invalid_arguments"
    assert gateway.calls[-1]["stage"] == "schema" and gateway.executed_calls("agent") == 0


def test_changed_file_and_past_eof_refuse_without_returning_new_bytes(tmp_path):
    data = b"first\r\nsecond"
    gateway, workspace, _ = _gateway(tmp_path, data)

    async def run():
        first = await _read(gateway, max_chars=2)
        assert first.value["content"] == "fi"
        normalized = await _read(
            gateway, offset=2, expected_sha256=sha256(data.replace(b"\r\n", b"\n")).hexdigest()
        )
        assert normalized.outcome.value == "rejected" and "changed" in normalized.public_message
        beyond = await _read(
            gateway, offset=len(data) + 1, expected_sha256=sha256(data).hexdigest()
        )
        assert beyond.outcome.value == "rejected" and "offset" in beyond.public_message
        workspace.resolve(PATH).write_bytes(b"replacement hidden")
        changed = await _read(gateway, offset=2, expected_sha256=first.value["sha256"])
        assert changed.outcome.value == "rejected" and "changed" in changed.public_message
        assert changed.value is None and "replacement hidden" not in _wire(changed)
        assert gateway.executed_calls("agent") == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "restriction,code",
    [
        ("identity", "tool_not_bound"),
        ("allowlist", "tool_not_allowed"),
        ("denied", "policy_denied"),
        ("budget", "tool_rate_limited"),
    ],
)
def test_continuation_rechecks_permissions_and_tool_budget(tmp_path, restriction, code):
    data = "原文".encode() * 2000
    gateway, _, binding = _gateway(tmp_path, data)

    async def run():
        first = await _read(gateway)
        assert first.outcome.value == "succeeded"
        if restriction == "identity":
            gateway.unbind("agent")
        elif restriction == "allowlist":
            gateway.bind("agent", replace(binding, allowed_tools=()))
        elif restriction == "denied":
            gateway.bind("agent", replace(binding, denied_prefixes=("sources/",)))
        else:
            gateway.executed_counter = lambda _: 1  # durable count after reopening the gateway
            gateway.bind("agent", replace(binding, max_tool_calls=1))
        second = await _read(
            gateway, offset=first.value["next_offset"], expected_sha256=first.value["sha256"]
        )
        assert second.error_code == code and second.value is None
        assert gateway.executed_calls("agent") == 1

    asyncio.run(run())


@pytest.mark.parametrize("bad", ["symlink", "oversize", "invalid_utf8"])
def test_paged_raw_read_preserves_filesystem_limits(tmp_path, bad):
    gateway, workspace, _ = _gateway(tmp_path, b"valid")
    target = workspace.resolve(PATH)
    if bad == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        target.unlink()
        target.symlink_to(outside)
    elif bad == "oversize":
        target.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    else:
        target.write_bytes(b"\xff\xfe")
    result = asyncio.run(_read(gateway, max_chars=1))
    assert result.outcome.value == "rejected" and result.value is None


def test_readonly_verification_pages_do_not_grant_write(tmp_path):
    data = ("报告\r\n" * 1500).encode("utf-8")
    gateway, _, _ = _gateway(tmp_path, data)
    gateway._workspaces.verification_copy("attempt")
    gateway.bind(
        "critic",
        WorkspaceBinding(
            "attempt",
            "verify",
            False,
            CRITIC_TOOLS,
            untrusted_sources=("sources/",),
        ),
    )
    first = asyncio.run(_read(gateway, agent="critic"))
    assert first.value["next_offset"] > 0
    second = asyncio.run(
        _read(
            gateway,
            agent="critic",
            offset=first.value["next_offset"],
            expected_sha256=first.value["sha256"],
        )
    )
    assert second.outcome.value == "succeeded"
    rejected = asyncio.run(
        gateway.execute(
            ToolCall(CallId("write"), "workspace_write_file", {"path": PATH, "content": "fake"}),
            {"run_id": "critic"},
        )
    )
    assert rejected.error_code == "tool_not_allowed"


def test_additive_tool_schema_describes_continuation_without_changing_path_contract():
    schema = TOOL_SCHEMAS["workspace_read_file"]
    assert schema["required"] == ["path"] and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"path", "offset", "max_chars", "expected_sha256"}
    description = str(schema)
    assert "next_offset" in description and "expected_sha256" in description


def test_empty_file_has_an_explicit_terminal_page(tmp_path):
    gateway, _, _ = _gateway(tmp_path, b"")
    result = asyncio.run(_read(gateway, offset=0, max_chars=4096))
    assert result.outcome.value == "succeeded"
    assert result.value["content"] == ""
    assert result.value["total_chars"] == 0 and result.value["next_offset"] is None
    assert result.value["start_line"] == result.value["end_line"] == 1
    assert not result.value["starts_mid_line"] and not result.value["ends_mid_line"]
