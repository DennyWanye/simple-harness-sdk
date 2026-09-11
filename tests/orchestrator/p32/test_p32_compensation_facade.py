# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice D · P32-8 / P32-11 (entry half): asking for a compensation from outside
(plan v3 D8; plan review round 2 P2-7).

A compensation is a person's decision, so the product asks for it through the one surface
it has.  The facade checks that the action and the Artifact really belong to this tenant's
Mission, binds the Artifact's identity itself, and hands the rest to the ledger; a foreign
action is ``not_found`` — its existence is not revealed.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.contracts import Artifact
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.action_commits import ActionCommitError

ME = Principal("local-user:me", "我")
REPORT = "# 周报\n".encode()
HASH = hashlib.sha256(REPORT).hexdigest()


def _artifact(mission_id: str = "mission-1", path: str = "report.md") -> Artifact:
    return Artifact(
        id="artifact-1",
        mission_id=mission_id,
        task_id="task-1",
        attempt_id="att-1",
        type="file",
        path=path,
        version=1,
        content_hash=HASH,
        size_bytes=len(REPORT),
        produced_by="att-1",
        storage_uri=f"/evidence/artifacts/sha256/{HASH}",
    )


class _Mission:
    def __init__(self, tenant: str = "local") -> None:
        self.id = "mission-1"
        self.tenant_id = tenant


class _Commit:
    def __init__(self, outcome: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcome = outcome

    def propose_compensation(self, action_key: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"action_key": action_key, **kwargs})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or {
            "action_key": f"{action_key}#comp-1:v1",
            "state": "AWAITING_APPROVAL",
        }


class _Store:
    def __init__(self, *, mission: _Mission | None, action: dict | None, artifact: Artifact | None):
        self._mission = mission
        self._action = action
        self._artifact = artifact

    def get_mission(self, mission_id: str) -> Any:
        return self._mission if self._mission and mission_id == self._mission.id else None

    def get_action(self, action_key: str) -> Any:
        return self._action if self._action and action_key == self._action["action_key"] else None

    def get_artifact(self, artifact_id: str) -> Any:
        return self._artifact if self._artifact and artifact_id == self._artifact.id else None

    def list_missions(self) -> list[Any]:
        return [self._mission] if self._mission else []


class _Config:
    deployment_policy = DeploymentPolicy(enabled_connectors=("test_config",))


class _Orchestrator:
    def __init__(self, store: _Store, commit: _Commit) -> None:
        self.store = store
        self.commit = commit
        self.config = _Config()
        self.connectors: dict[str, Any] = {}


def _control(
    *,
    tenant: str = "local",
    action: dict | None = None,
    artifact: Artifact | None = None,
    outcome: Any = None,
):
    action = (
        action if action is not None else {"action_key": "action-1:v1", "mission_id": "mission-1"}
    )
    artifact = artifact if artifact is not None else _artifact()
    commit = _Commit(outcome)
    store = _Store(mission=_Mission(tenant), action=action, artifact=artifact)
    control = MissionControlV1(_Orchestrator(store, commit), tenant_id="local", principal=ME)
    return control, commit


def test_the_facade_binds_the_artifact_identity_itself():
    control, commit = _control()
    proposed = control.propose_compensation(
        "action-1:v1", operation="publish", artifact_id="artifact-1", reason="上一版内容错了"
    )
    assert proposed["state"] == "AWAITING_APPROVAL"
    [call] = commit.calls
    assert call["params"] == {
        "artifact_path": "report.md",
        "artifact_id": "artifact-1",
        "content_hash": HASH,
        "size": len(REPORT),
        "storage_uri": f"/evidence/artifacts/sha256/{HASH}",
    }
    assert call["artifact_hash"] == HASH and call["operation"] == "publish"


def test_a_caller_may_not_state_the_identity():
    control, commit = _control()
    with pytest.raises(FacadeError) as refused:
        control.propose_compensation(
            "action-1:v1",
            operation="publish",
            artifact_id="artifact-1",
            reason="试图自己写 hash",
            params={"content_hash": "0" * 64},
        )
    assert refused.value.code == "refused" and commit.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action_key": "  ", "operation": "publish", "reason": "空动作"},
        {"action_key": "action-1:v1", "operation": " ", "reason": "空操作"},
        {"action_key": "action-1:v1", "operation": "publish", "reason": "   "},
    ],
    ids=["no-action", "no-operation", "no-reason"],
)
def test_an_incomplete_request_is_refused(kwargs):
    control, commit = _control()
    with pytest.raises(FacadeError) as refused:
        control.propose_compensation(artifact_id="artifact-1", **kwargs)
    assert refused.value.code == "invalid_request" and commit.calls == []


def test_a_foreign_mission_is_not_found():
    control, commit = _control(tenant="someone-else")
    with pytest.raises(FacadeError) as refused:
        control.propose_compensation(
            "action-1:v1", operation="publish", artifact_id="artifact-1", reason="别人的任务"
        )
    assert refused.value.code == "not_found" and commit.calls == []


def test_an_unknown_action_or_artifact_is_not_found():
    control, commit = _control()
    for action_key, artifact_id in (("action-9:v1", "artifact-1"), ("action-1:v1", "artifact-9")):
        with pytest.raises(FacadeError) as refused:
            control.propose_compensation(
                action_key, operation="publish", artifact_id=artifact_id, reason="找不到"
            )
        assert refused.value.code == "not_found"
    assert commit.calls == []


def test_an_artifact_of_another_mission_is_not_found():
    control, commit = _control(artifact=_artifact(mission_id="mission-2"))
    with pytest.raises(FacadeError) as refused:
        control.propose_compensation(
            "action-1:v1", operation="publish", artifact_id="artifact-1", reason="别的任务的产物"
        )
    assert refused.value.code == "not_found" and commit.calls == []


def test_a_secret_in_the_reason_is_refused():
    control, commit = _control()
    # built here, never written as a literal: a key-shaped string does not belong in a repo
    key_shaped = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    with pytest.raises(FacadeError) as refused:
        control.propose_compensation(
            "action-1:v1",
            operation="publish",
            artifact_id="artifact-1",
            reason=f"用这个密钥重发 {key_shaped}",
        )
    assert refused.value.code == "secret_rejected" and commit.calls == []


def test_a_refusal_from_the_ledger_reaches_the_caller():
    control, _commit = _control(
        outcome=ActionCommitError("only a SUCCEEDED action can be compensated")
    )
    with pytest.raises(FacadeError) as refused:
        control.propose_compensation(
            "action-1:v1", operation="publish", artifact_id="artifact-1", reason="还没执行"
        )
    assert refused.value.code == "refused" and "SUCCEEDED" in str(refused.value)
