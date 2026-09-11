# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice A · P32-4: ``code_execution`` = off / sandboxed / process_only, the legacy
``local_code_execution`` switch, the executor a deployment resolves to, and the soft
limits (RSS, process count) that are enforced by sampling and reaping — never claimed as
hard limits (plan v3 D1 / D2; plan review round 1 P1-3, round 2 P2-8).

The SDK default stays ``process_only`` (registered deviation from Phase3 §4.3, whose
"production" is the Host); every receipt of that mode says ``isolated=False``.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from agent_orchestrator.governance.policies import (
    SNAPSHOT_FIELDS,
    DeploymentPolicy,
    deployed_layers,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig, assemble_orchestrator_runtime
from agent_orchestrator.runtime.sandbox import (
    ProcessOnlyExecutor,
    SandboxSpec,
    SandboxUnavailable,
    SeatbeltExecutor,
    probe_sandbox,
    resolve_executor,
)
from agent_orchestrator.runtime.tool_gateway import run_pytest
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

SEATBELT = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists()
TOOLS3 = ("workspace_read_file", "workspace_write_file", "workspace_list")


def _run(coro, limit=90.0):
    return asyncio.run(asyncio.wait_for(coro, timeout=limit))


# ------------------------------------------------------------------ policy semantics
def test_default_deployment_is_process_only_and_unchanged():
    policy = DeploymentPolicy()
    assert policy.code_execution == "process_only"
    assert policy.local_code_execution is True
    assert "code_test" in deployed_layers(policy)


def test_legacy_switch_off_maps_to_off():
    policy = DeploymentPolicy(allowed_tools=TOOLS3, local_code_execution=False)
    assert policy.code_execution == "off"
    assert "code_test" not in deployed_layers(policy)


@pytest.mark.parametrize(
    "mode,local", [("off", False), ("sandboxed", True), ("process_only", True)]
)
def test_code_execution_modes(mode, local):
    tools = TOOLS3 if mode == "off" else (*TOOLS3, "run_tests")
    policy = DeploymentPolicy(allowed_tools=tools, code_execution=mode)
    assert policy.code_execution == mode
    assert policy.local_code_execution is local
    assert policy.to_json()["code_execution"] == mode
    assert ("code_test" in deployed_layers(policy)) is local


def test_contradictions_are_refused():
    with pytest.raises(ValueError):
        DeploymentPolicy(
            allowed_tools=TOOLS3, local_code_execution=False, code_execution="sandboxed"
        )
    with pytest.raises(ValueError):
        DeploymentPolicy(code_execution="container")  # unknown mode
    with pytest.raises(ValueError):
        DeploymentPolicy(code_execution="off")  # run_tests still allowed


def test_the_executor_is_a_runtime_object_outside_the_policy_snapshot():
    assert SNAPSHOT_FIELDS["sandbox_executor"].startswith("excluded")


# ------------------------------------------------------------------ resolving the executor
def test_off_resolves_to_no_executor():
    policy = DeploymentPolicy(allowed_tools=TOOLS3, code_execution="off")
    assert resolve_executor(policy, None) is None


def test_process_only_defaults_to_the_process_executor():
    executor = resolve_executor(DeploymentPolicy(), None)
    assert isinstance(executor, ProcessOnlyExecutor) and executor.isolated is False


def test_sandboxed_without_a_probed_seatbelt_is_refused():
    policy = DeploymentPolicy(code_execution="sandboxed")
    with pytest.raises(SandboxUnavailable):
        resolve_executor(policy, None)
    with pytest.raises(SandboxUnavailable):
        resolve_executor(policy, ProcessOnlyExecutor())  # not a sandbox at all
    if SEATBELT:
        with pytest.raises(SandboxUnavailable):
            resolve_executor(policy, SeatbeltExecutor.for_interpreter())  # never probed


@pytest.mark.skipif(not SEATBELT, reason="seatbelt needs macOS sandbox-exec")
def test_sandboxed_with_a_probed_seatbelt_is_accepted():
    executor = SeatbeltExecutor.for_interpreter()
    assert _run(probe_sandbox(executor), limit=120).ok
    assert resolve_executor(DeploymentPolicy(code_execution="sandboxed"), executor) is executor


def test_sandboxed_orchestrator_config_refuses_a_missing_executor(tmp_path):
    config = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence",
        deployment_policy=DeploymentPolicy(code_execution="sandboxed"),
    )
    with pytest.raises(SandboxUnavailable):
        assemble_orchestrator_runtime(config, RoleScriptedProvider({}))


# ------------------------------------------------------------------ run_pytest through the port
def test_run_pytest_carries_the_receipt(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    run = _run(run_pytest(str(tmp_path), path=None, timeout=60))
    assert run.passed, run.stdout
    assert run.receipt is not None and run.receipt.kind == "process_only"
    receipt = run.to_json()["receipt"]
    assert receipt["tree_killed"] is True and receipt["isolated"] is False


def test_run_pytest_reports_a_failing_test_as_failed(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    run = _run(run_pytest(str(tmp_path), path=None, timeout=60))
    assert not run.passed and run.returncode not in (0, None)


# ------------------------------------------------------------------ soft limits
def _script(root: Path, body: str) -> list[str]:
    path = root / "job.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def test_rss_soft_limit_reaps_the_run(tmp_path):
    command = _script(
        tmp_path,
        """
        import time
        block = bytearray(400 * 1024 * 1024)
        for i in range(0, len(block), 4096):
            block[i] = 1
        time.sleep(30)
        """,
    )
    spec = SandboxSpec(wall_seconds=30, max_rss_bytes=100 * 1024 * 1024)
    receipt = _run(ProcessOnlyExecutor().execute(command, cwd=str(tmp_path), spec=spec))
    assert receipt.limit_exceeded == "rss"
    assert receipt.tree_killed is True
    assert receipt.effective_limits["max_rss_bytes"]["enforcement"] == "soft"


def test_process_count_soft_limit_reaps_the_run(tmp_path):
    command = _script(
        tmp_path,
        """
        import subprocess, time
        children = [subprocess.Popen(["/bin/sleep", "60"]) for _ in range(20)]
        time.sleep(30)
        """,
    )
    spec = SandboxSpec(wall_seconds=30, max_processes=5)
    receipt = _run(ProcessOnlyExecutor().execute(command, cwd=str(tmp_path), spec=spec))
    assert receipt.limit_exceeded == "processes"
    assert receipt.tree_killed is True and receipt.residual_pids == ()
