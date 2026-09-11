# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Host support 0.9.8 · SA-5: ``Orchestrator.create_mission`` is the one door that knows
the deployment (Host plan §3.1 S1-b, plan review P1-1): parse → ``validate_spec`` against
the deployment's tools → action criteria → local-code-execution check → Commit with the
provider kind and the policy binding ``submit_mission`` uses.  ``MissionApi`` bound to
an orchestrator and the CLI ``mission create`` use it too."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_orchestrator.api.missions import MissionApi, MissionRequestError
from agent_orchestrator.contracts import Budget
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

TOOLS3 = ("workspace_read_file", "workspace_write_file", "workspace_list")
OFF = DeploymentPolicy(allowed_tools=TOOLS3, local_code_execution=False)


def _config(tmp_path, deployment=OFF):
    return OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, deployment_policy=deployment
    )


def _request(key, **overrides):
    request = {
        "goal": "写一份 NOTES.md，列出三个要点",
        "success_criteria": ["file:NOTES.md"],
        "idempotency_key": key,
        "budget": {"max_tokens": 200_000, "max_attempts": 4},
    }
    request.update(overrides)
    return request


def _with_orchestrator(tmp_path, body, deployment=OFF):
    async def run():
        async with Orchestrator(
            _config(tmp_path, deployment), RoleScriptedProvider({})
        ) as orchestrator:
            result = body(orchestrator)
            if asyncio.iscoroutine(result):
                result = await result
            return result

    return asyncio.run(run())


def test_create_is_idempotent_and_binds_like_submit(tmp_path):
    async def body(orchestrator):
        first, created = orchestrator.create_mission(tenant_id="t", request=_request("a"))
        again, created_again = orchestrator.create_mission(tenant_id="t", request=_request("a"))
        via_submit = await orchestrator.submit_mission(
            MissionSpec(
                goal="写一份 NOTES.md，列出三个要点",
                success_criteria=("file:NOTES.md",),
                tenant_id="t",
                idempotency_key="b",
                allowed_tools=TOOLS3,
                budget=Budget(max_tokens=200_000, max_attempts=4),
            )
        )
        store = orchestrator.store
        return (
            first,
            created,
            again,
            created_again,
            store.get_mission_policy(first.id),
            store.get_mission_policy(via_submit.id),
            len(store.list_missions()),
        )

    first, created, again, created_again, bound, bound_submit, count = _with_orchestrator(
        tmp_path, body
    )
    assert created is True and created_again is False and again.id == first.id
    assert count == 2
    assert bound["provider_kind"] == "fixtures"  # not "unknown"
    assert bound["version_id"] == bound_submit["version_id"]
    # an omitted tool set means the deployment's, not the SDK's four
    assert set(first.allowed_tools) == set(TOOLS3)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"success_criteria": ["pytest:tests"]}, "local code execution"),
        ({"success_criteria": ["file:NOTES.md", "action:test_config.set:x"]}, "action criterion"),
        ({"allowed_tools": [*TOOLS3, "run_tests"]}, "not offered"),
        ({"goal": "  "}, "goal"),
        ({"success_criteria": []}, "success_criteria"),
    ],
)
def test_the_door_refuses_and_writes_nothing(tmp_path, overrides, fragment):
    def body(orchestrator):
        with pytest.raises(MissionRequestError, match=fragment):
            orchestrator.create_mission(tenant_id="t", request=_request("refused", **overrides))
        return len(orchestrator.store.list_missions())

    assert _with_orchestrator(tmp_path, body) == 0


def test_pytest_criteria_are_accepted_when_local_code_execution_is_on(tmp_path):
    def body(orchestrator):
        mission, created = orchestrator.create_mission(
            tenant_id="t", request=_request("on", success_criteria=["pytest:tests"])
        )
        return created

    assert _with_orchestrator(tmp_path, body, deployment=DeploymentPolicy()) is True


def test_mission_api_bound_to_the_orchestrator_uses_the_same_door(tmp_path):
    def body(orchestrator):
        api = MissionApi(orchestrator.commit, orchestrator=orchestrator)
        with pytest.raises(MissionRequestError, match="local code execution"):
            api.create(tenant_id="t", request=_request("x", success_criteria=["pytest:tests"]))
        mission, created = api.create(tenant_id="t", request=_request("y"))
        return created, orchestrator.store.get_mission_policy(mission.id)

    created, bound = _with_orchestrator(tmp_path, body)
    assert created is True and bound["provider_kind"] == "fixtures"


def test_cli_mission_create_records_the_provider_kind(tmp_path):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(_request("cli"), ensure_ascii=False), encoding="utf-8")
    evidence = tmp_path / "cli-evidence"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_orchestrator",
            "mission",
            "create",
            "--spec",
            str(spec_file),
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--tenant",
            "t",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    printed, _ = json.JSONDecoder().raw_decode(result.stdout)
    store = Store.open(evidence / "orchestrator.db")
    try:
        assert store.get_mission_policy(printed["mission_id"])["provider_kind"] == "fixtures"
    finally:
        store.close()
