# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · slice B2/B5: isolated workspaces refuse escapes; the tool gateway confines
every tool to the bound Attempt and view; ``run_tests`` really runs pytest in a child
process; tagged output blocks parse strictly."""

from __future__ import annotations

import asyncio

import pytest

from agent_orchestrator.artifacts.workspace import WorkspaceError, WorkspaceManager
from agent_orchestrator.runtime.output_blocks import BlockError, extract_block, outside_text
from agent_orchestrator.runtime.tool_gateway import (
    CRITIC_TOOLS,
    WORKER_TOOLS,
    WorkspaceBinding,
    WorkspaceToolGateway,
)
from simple_harness.contracts import CallId
from simple_harness.tools import ToolCall

SEED = {
    "parse_kv.py": "def parse_kv(text):\n    raise NotImplementedError\n",
    "tests/test_parse_kv.py": "from parse_kv import parse_kv\n\n\ndef test_basic():\n    assert parse_kv('a=1;b=2') == {'a': '1', 'b': '2'}\n",
}


def test_workspace_paths_are_confined(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create("attempt-1", seed=SEED)
    assert workspace.list_files() == ["parse_kv.py", "tests/test_parse_kv.py"]
    for bad in ("../x", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(WorkspaceError):
            workspace.resolve(bad)
    (tmp_path / "outside.txt").write_text("secret")
    (workspace.root / "link").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(WorkspaceError):
        workspace.read_text("link")
    assert "link" not in workspace.list_files()
    artifacts = workspace.snapshot(mission_id="m", task_id="t", produced_by="agent")
    assert {a.path for a in artifacts} == {"parse_kv.py", "tests/test_parse_kv.py"}
    assert all(len(a.content_hash) == 64 for a in artifacts)
    # verification copy is independent of later worker writes
    copy = manager.verification_copy("attempt-1")
    workspace.write_text("parse_kv.py", "changed")
    assert copy.read_text("parse_kv.py") == SEED["parse_kv.py"]
    with pytest.raises(WorkspaceError):
        manager.verification_view("attempt-1").write_text("x.py", "")
    # a repair attempt inherits the previous tree
    repair = manager.create("attempt-2", seed=SEED, previous=workspace.root)
    assert repair.read_text("parse_kv.py") == "changed"


def test_gateway_confines_tools_and_runs_pytest(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    manager.create("attempt-1", seed=SEED)
    gateway = WorkspaceToolGateway(manager, test_timeout=60)
    gateway.bind("agent-w", WorkspaceBinding("attempt-1", "work", True, WORKER_TOOLS))

    async def call(run_id, name, **arguments):
        return await gateway.execute(ToolCall(CallId("c1"), name, arguments), {"run_id": run_id})

    async def case():
        unbound = await call("agent-x", "workspace_list")
        assert unbound.outcome.value == "rejected" and unbound.error_code == "tool_not_bound"
        listed = await call("agent-w", "workspace_list")
        assert list(listed.value["files"]) == ["parse_kv.py", "tests/test_parse_kv.py"]
        escape = await call("agent-w", "workspace_read_file", path="../secret")
        assert escape.error_code == "workspace_error"
        failing = await call("agent-w", "run_tests", path="tests/test_parse_kv.py")
        assert failing.value["passed"] is False and failing.value["returncode"] != 0
        assert "NotImplementedError" in failing.value["stdout"]
        written = await call(
            "agent-w",
            "workspace_write_file",
            path="parse_kv.py",
            content="def parse_kv(text):\n    return dict(p.split('=', 1) for p in text.split(';') if p)\n",
        )
        assert written.value["bytes"] > 0
        passing = await call("agent-w", "run_tests")
        assert passing.value["passed"] is True, passing.value["stdout"]
        # critic view: read-only on the verification copy
        manager.verification_copy("attempt-1")
        gateway.bind("agent-c", WorkspaceBinding("attempt-1", "verify", False, CRITIC_TOOLS))
        denied = await call("agent-c", "workspace_write_file", path="x.py", content="")
        assert denied.error_code == "tool_not_allowed"
        read = await call("agent-c", "workspace_read_file", path="parse_kv.py")
        assert "return dict" in read.value["content"]
        assert [c["outcome"] for c in gateway.calls].count("succeeded") == 5

    asyncio.run(case())


def test_output_block_parsing_is_strict():
    text = 'prose\n<result_envelope>\n{"a": 1}\n</result_envelope>\nmore'
    assert extract_block(text, "result_envelope") == {"a": 1}
    assert outside_text(text, "result_envelope") == "prose\n\nmore"
    fenced = '<result_envelope>```json\n{"a": 1}\n```</result_envelope>'
    assert extract_block(fenced, "result_envelope") == {"a": 1}
    for bad, reason in (
        ("", "empty_output"),
        ("no block", "block_missing"),
        ("<x>{}</x><x>{}</x>", "block_ambiguous"),
        ("<x>{oops}</x>", "invalid_json"),
        ("<x>[1]</x>", "not_an_object"),
    ):
        with pytest.raises(BlockError) as exc:
            extract_block(bad, "x")
        assert exc.value.reason == reason
