"""Focused checks for the read-only OperationWorkspace projection."""
from __future__ import annotations

from types import SimpleNamespace

from agent_orchestrator.api.operation_workspace import operation_workspace
from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore

from test_scoped_content_commit import _mixed_world


def test_workspace_returns_the_committed_spec_without_mutating_the_tenant_store(tmp_path) -> None:
    world, requirements, approved, *_ = _mixed_world(tmp_path, with_output=True)
    PlanningDecisionStore(world.store).bind_mission_protocol(
        world.mission.id,
        protocol_version="planning-decision-v1",
        package_version=6,
        prompt_version="planner-hierarchical-v9",
        binding_hash="a" * 64,
    )
    loop = SimpleNamespace(
        store=world.store,
        commit=world.service,
        connectors={},
        assembled=SimpleNamespace(
            workspaces=SimpleNamespace(artifact_store=ArtifactStore(tmp_path / "projection-cas"))
        ),
    )
    before = world.store.connection.total_changes

    projected = operation_workspace(loop, world.mission, principal=Principal("tenant-user"))

    assert projected is not None
    assert projected["mission_id"] == world.mission.id
    assert projected["state"] == "APPROVED"
    assert projected["requirements_ref"] == {
        "kind": "requirements",
        "id": str(requirements.revision_id),
        "revision": requirements.revision,
        "content_hash": requirements.content_hash(),
    }
    assert projected["spec_hash"] == approved.spec_hash
    assert projected["spec"]["requirements_ref"] == {
        key: value for key, value in approved.requirements_ref.to_json().items() if key != "kind"
    }
    assert projected["spec"]["mission_id"] == world.mission.id
    assert {item["id"] for item in projected["criteria"]} == {
        criterion.criterion_id for criterion in requirements.criteria
    }
    assert world.store.connection.total_changes == before


def test_foreign_tenant_cannot_reach_operation_workspace_through_the_facade(tmp_path) -> None:
    world, *_ = _mixed_world(tmp_path, with_output=True)
    loop = SimpleNamespace(
        store=world.store,
        commit=world.service,
        connectors={},
        config=SimpleNamespace(deployment_policy=SimpleNamespace()),
        assembled=SimpleNamespace(
            workspaces=SimpleNamespace(artifact_store=ArtifactStore(tmp_path / "foreign-cas"))
        ),
    )
    foreign = MissionControlV1(
        loop, tenant_id="another-tenant", principal=Principal("foreign-user")
    )
    before = world.store.connection.total_changes

    try:
        foreign.snapshot(world.mission.id)
    except FacadeError as error:
        assert error.code == "not_found"
        assert str(error) == "no such object for this caller"
    else:
        raise AssertionError("foreign tenant read unexpectedly succeeded")
    assert world.store.connection.total_changes == before
