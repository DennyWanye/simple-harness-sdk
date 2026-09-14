"""Third-domain dispatch and real tool gateway boundary; no benchmark answers."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_orchestrator.contracts import ContractError
from agent_orchestrator.governance.domains import APPWORLD_PROFILE, CODE_PROFILE, DOC_PROFILE
from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain
from agent_orchestrator.verification.domain_handlers import handler_for


def test_appworld_capability_snapshot_is_stable_and_does_not_serialize_callback(tmp_path):
    from agent_orchestrator.governance.policies import policy_snapshot
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    config = OrchestratorConfig(evidence_root=tmp_path)
    disabled = policy_snapshot(config)
    enabled = policy_snapshot(replace(config, appworld_execute=lambda code: code))
    equivalent = policy_snapshot(replace(config, appworld_execute=lambda code: None))
    assert disabled["config"]["appworld_execute"] is False
    assert enabled["config"]["appworld_execute"] is True
    assert enabled["hash"] == equivalent["hash"] != disabled["hash"]


def test_third_domain_is_explicit_and_unknown_does_not_become_document():
    assert handler_for(CODE_PROFILE).name == "code"
    assert handler_for(DOC_PROFILE).name == "document"
    assert handler_for(APPWORLD_PROFILE).name == "appworld"
    assert not handler_for(APPWORLD_PROFILE).document_assessments
    with pytest.raises(ContractError, match="No verification handler"):
        handler_for(replace(APPWORLD_PROFILE, id="unknown"))


def test_appworld_templates_never_expose_evaluator_or_pytest_tool():
    for role in APPWORLD_PROFILE.role_templates:
        template = template_for_domain(ROLES[role], APPWORLD_PROFILE, {})
        assert "run_tests" not in template.tool_names
        assert not any("evaluat" in name for name in template.tool_names)
        if role not in {"planner", "manager", "critic"}:
            assert "appworld_execute" in template.tool_names


@pytest.mark.asyncio
async def test_actual_gateway_shares_one_world_and_refuses_other_missions(tmp_path):
    import asyncio
    import time

    from agent_orchestrator.artifacts.workspace import WorkspaceError, WorkspaceManager
    from agent_orchestrator.runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway
    from simple_harness.contracts import CallId
    from simple_harness.tools import ToolCall

    state = {"value": 0, "active": 0, "peak": 0}

    def execute(code):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.01)
        state["value"] += int(code)
        state["active"] -= 1
        return {"output": str(state["value"])}

    manager = WorkspaceManager(tmp_path)
    gateway = WorkspaceToolGateway(manager, appworld_execute=execute)
    gateway.bind_appworld("m1")
    for name, mission in (("a", "m1"), ("b", "m1"), ("c", "m2")):
        manager.create(name, seed={})
        gateway.bind(
            name, WorkspaceBinding(name, "work", True, ("appworld_execute",), mission_id=mission)
        )

    async def call(run):
        return await gateway.execute(
            ToolCall(CallId(run), "appworld_execute", {"code": "1"}), {"run_id": run}
        )

    await asyncio.gather(call("a"), call("b"))
    assert state["value"] == 2 and state["peak"] == 1
    await call("c")
    assert state["value"] == 2
    assert gateway.calls[-1]["outcome"].startswith("rejected:")
    with pytest.raises(WorkspaceError, match="across Missions"):
        gateway.bind_appworld("m2")


@pytest.mark.asyncio
async def test_cancelled_appworld_call_keeps_physical_lock_until_thread_finishes(tmp_path):
    import asyncio
    from threading import Event

    from agent_orchestrator.artifacts.workspace import WorkspaceManager
    from agent_orchestrator.runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway
    from simple_harness.contracts import CallId
    from simple_harness.tools import ToolCall

    entered, release, second_started = Event(), Event(), Event()

    def execute(code):
        if code == "first":
            entered.set()
            assert release.wait(3)
        else:
            second_started.set()
        return {"output": code}

    manager = WorkspaceManager(tmp_path)
    gateway = WorkspaceToolGateway(manager, appworld_execute=execute)
    gateway.bind_appworld("m")
    for name in ("first", "second"):
        manager.create(name, seed={})
        gateway.bind(
            name, WorkspaceBinding(name, "work", True, ("appworld_execute",), mission_id="m")
        )

    async def call(name):
        return await gateway.execute(
            ToolCall(CallId(name), "appworld_execute", {"code": name}), {"run_id": name}
        )

    first = asyncio.create_task(call("first"))
    assert await asyncio.to_thread(entered.wait, 2)
    first.cancel()
    second = asyncio.create_task(call("second"))
    try:
        await asyncio.sleep(0.05)
        assert not second_started.is_set()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    assert second_started.is_set()
