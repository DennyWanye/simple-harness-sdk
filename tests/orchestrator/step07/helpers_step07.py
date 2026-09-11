# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Shared builders for the step-7 tests: a bare Commit Service with one Mission / Task /
accepted candidate, the test configuration service and the people who decide."""

from __future__ import annotations

from pathlib import Path

from agent_orchestrator.contracts import Budget
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.runtime.connectors import PaymentConnectorStub, TestConfigService

ALICE = Principal("alice", "Alice")
BOB = Principal("bob", "Bob")

# D7-3': a deployment enables nothing by default; the tests enable the test service
ENABLED = DeploymentPolicy(enabled_connectors=("test_config",))
# the Mission charter's action scope (its action criteria) for the ledger tests
LEDGER_CRITERIA = (
    "file:CHANGE.md",
    "action:test_config.set:feature_flags.new_ui",
    "action:test_config.read:feature_flags.new_ui",
    "action:test_config.delete:feature_flags.new_ui",
    "action:test_config.set:a",
    "action:test_config.set:b",
    "action:test_config.set:c",
)


def ledger_service(
    tmp_path, *, deployment: DeploymentPolicy | None = None, clock=None, criteria=LEDGER_CRITERIA
):
    """A Commit Service with one ACTIVE Mission and one Task, plus the connectors."""

    from graph_helpers7 import graph_service  # noqa: PLC0415 - test-local helper

    service, mission, tasks = graph_service(tmp_path, clock=clock, success_criteria=criteria)
    config = TestConfigService(Path(tmp_path) / "test-services" / "config.json")
    connectors = {"test_config": config, "payment": PaymentConnectorStub()}
    return service, mission, tasks, config, connectors, deployment or ENABLED


def candidate(
    target="feature_flags.new_ui", value="on", *, operation="set", connector="test_config"
):
    params = {} if operation in {"read", "delete"} else {"value": value}
    return {
        "connector": connector,
        "operation": operation,
        "target": target,
        "params": params,
        "reason": "按需求修改测试配置",
    }


def spec(key="s7", **overrides):
    base = dict(
        goal="修改测试配置服务的一项配置并记录变更说明",
        success_criteria=("file:CHANGE.md",),
        tenant_id="tenant-7",
        idempotency_key=key,
        allowed_tools=(
            "workspace_read_file",
            "workspace_write_file",
            "workspace_list",
            "run_tests",
        ),
        budget=Budget(max_tokens=300_000, max_attempts=12),
    )
    base.update(overrides)
    return MissionSpec(**base)


__all__ = ("ALICE", "BOB", "ENABLED", "LEDGER_CRITERIA", "candidate", "ledger_service", "spec")
