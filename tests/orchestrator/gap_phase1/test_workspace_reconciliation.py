"""Lost SDK responses can use exact durable Host write proof; never blind retries."""

from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.runtime.tool_gateway import WorkspaceToolGateway
from simple_harness.contracts import CallId, RunId
from simple_harness.tools.reconciliation import ReconciliationState


@pytest.mark.asyncio
async def test_known_write_reconciles_without_reexecution_and_drift_stays_unknown(tmp_path):
    manager = WorkspaceManager(tmp_path)
    workspace = manager.create("attempt", seed={"report.md": "precise text\n"})
    gateway = WorkspaceToolGateway(manager)
    effect = SimpleNamespace(
        run_id=RunId("agent"),
        call_id=CallId("call"),
        tool_name="workspace_write_file",
        arguments={"path": "report.md", "content": "precise text\n"},
    )
    proof = {
        "subject_id": "attempt",
        "tool": "workspace_write_file",
        "outcome": "succeeded",
        "agent_id": "agent",
    }
    gateway.executed_lookup = lambda key: proof if key == "agent:call" else None
    before = (workspace.root / "report.md").stat().st_mtime_ns
    observed = await gateway.observe(effect)
    assert observed.state is ReconciliationState.COMPLETED
    assert observed.result.value == {"path": "report.md", "bytes": 13}
    assert (workspace.root / "report.md").stat().st_mtime_ns == before
    workspace.write_text("report.md", "changed")
    assert (await gateway.observe(effect)).state is ReconciliationState.STILL_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["no_proof", "wrong_agent", "wrong_tool", "failed", "other_call", "external"]
)
async def test_missing_foreign_or_external_evidence_cannot_settle_effect(tmp_path, case):
    manager = WorkspaceManager(tmp_path)
    manager.create("attempt", seed={"report.md": "match"})
    gateway = WorkspaceToolGateway(manager)
    proof = {
        "subject_id": "attempt",
        "tool": "workspace_write_file",
        "outcome": "succeeded",
        "agent_id": "agent",
    }
    if case == "wrong_agent":
        proof["agent_id"] = "foreign"
    if case == "wrong_tool":
        proof["tool"] = "workspace_read_file"
    if case == "failed":
        proof["outcome"] = "failed"
    gateway.executed_lookup = lambda key: (
        proof if key == "agent:call" and case != "no_proof" else None
    )
    effect = SimpleNamespace(
        run_id=RunId("agent"),
        call_id=CallId("other" if case == "other_call" else "call"),
        tool_name="appworld_execute" if case == "external" else "workspace_write_file",
        arguments={"path": "report.md", "content": "match"},
    )
    assert (await gateway.observe(effect)).state is ReconciliationState.STILL_UNKNOWN
