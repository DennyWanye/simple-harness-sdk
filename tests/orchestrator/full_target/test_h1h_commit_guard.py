from __future__ import annotations

import dataclasses
import hashlib

import pytest
from test_h1i_production_entry import _approve_root_content_only_spec
from test_htn_store import envelope
from test_plan_commits import _world

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.planning_decisions import (
    PLANNING_DECISION_V1,
    PlanningRequestBinding,
)
from agent_orchestrator.contracts.semantic_base import content_hash_of
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.planning_authorization import (
    PlanningAuthorizationSnapshot,
    planning_policy_for_mission,
    StorePlanningAuthorityReader,
    build_planning_authorization,
)
from agent_orchestrator.orchestrator.plan_commits import PlanCommitRejected
from agent_orchestrator.orchestrator.planning_admission_commits import (
    PlanningCommitAdmission,
)
from agent_orchestrator.orchestrator.planning_protocol_binding import bind_planning_protocol
from agent_orchestrator.planning.plan_preview import _source_snapshot_payload
from agent_orchestrator.runtime.planning_operations import (
    OperationEffect,
    StoreOperationReader,
    build_operation_snapshot,
    read_running_work,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from simple_harness.contracts import canonical_json


def _setup(world, *, command=None, bind_protocol: bool = True, dispatchable: bool = False):
    if bind_protocol:
        bind_planning_protocol(world.store, world.mission.id, PLANNING_DECISION_V1)
    command = command or world.command
    if dispatchable:
        requirements = _approve_root_content_only_spec(
            world.service, world.mission, world.binding,
            command_id="approve-completion-guard-positive-control",
        )
        command = dataclasses.replace(command, delta=dataclasses.replace(
            command.delta, read_set=dataclasses.replace(
                command.read_set, requirements_revision=int(requirements.revision))))
        world.command = command
    request = PlanningRequestBinding(
        request_id="request-h1h-guard",
        mission_id=world.mission.id,
        protocol_version=PLANNING_DECISION_V1,
        package_version=6,
        package_hash="a" * 64,
        base_plan_revision=0,
        requirements_revision=int(command.read_set.requirements_revision),
        scope_epoch_digest="b" * 64,
        subject_bindings_hash="c" * 64,
        visible_refs_digest="d" * 64,
        prompt_version="planner-hierarchical-v9",
        prompt_hash="e" * 64,
        created_at=world.store.now,
        intent_id="intent-h1h-guard",
    )
    PlanningDecisionStore(world.store).insert_planning_request(request)
    api = PlanningAuthorizationApi(
        world.store,
        tenant_id=world.mission.tenant_id,
        principal=Principal(world.principal.principal_id),
    )
    grant = api.issue(
        world.mission.id,
        command_id="grant-h1h-guard",
        request_id=request.request_id,
    )
    authority = build_planning_authorization(
        request.request_id,
        read=StorePlanningAuthorityReader(PlanningAdmissionStore(world.store), world.store),
        caller=world.principal,
        policy=planning_policy_for_mission(world.store, world.mission.id),
        now_ms=int(world.store.now * 1000),
    )
    assert isinstance(authority, PlanningAuthorizationSnapshot)
    operations = build_operation_snapshot(
        world.mission.id,
        reader=StoreOperationReader(world.store),
    )
    runtime_work = read_running_work(
        world.mission.id,
        (),
        reader=StoreOperationReader(world.store),
    )
    compilation_hash = hashlib.sha256(
        canonical_json(
            {
                "delta": command.delta.to_json(),
                "network": _source_snapshot_payload(command.network),
            }
        )
        .encode("utf-8")
    ).hexdigest()
    admission = PlanningCommitAdmission(
        request_id=request.request_id,
        decision_hash="f" * 64,
        decision_key="REFINE",
        authority=authority,
        operations=operations,
        runtime_work=runtime_work,
        preview_request_id=request.request_id,
        preview_decision_hash="f" * 64,
        preview_compilation_hash=compilation_hash,
        preview_read_set_hash=content_hash_of(command.read_set.to_json()),
    )
    return api, grant, admission


def _plan_revision_count(world) -> int:
    return len(HtnStore(world.store).list_plan_revisions(world.mission.id))


def test_a05_expired_grant_is_rechecked_inside_commit_and_rolls_back(tmp_path) -> None:
    world = _world(tmp_path, key="h1h-a05")
    _, _, admission = _setup(world, dispatchable=True)
    before_events = tuple(world.store.list_events(world.mission.id))
    before_changes = world.store.connection.total_changes
    world.store._clock = lambda: (admission.authority.expires_at_ms + 1) / 1000

    with pytest.raises(PlanCommitRejected, match="AUTHORIZATION_REQUIRED"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _plan_revision_count(world) == 0
    assert tuple(world.store.list_events(world.mission.id)) == before_events
    assert world.store.connection.total_changes == before_changes


def test_a08_replay_returns_original_receipt_after_revoke_without_new_revision(tmp_path) -> None:
    world = _world(tmp_path, key="h1h-a08")
    bind_planning_protocol(world.store, world.mission.id, PLANNING_DECISION_V1)
    requirements = _approve_root_content_only_spec(
        world.service,
        world.mission,
        world.binding,
        command_id="approve-completion-h1h-a08",
    )
    command = dataclasses.replace(
        world.command,
        delta=dataclasses.replace(
            world.command.delta,
            read_set=dataclasses.replace(
                world.command.read_set,
                requirements_revision=int(requirements.revision),
            ),
        ),
    )
    api, grant, admission = _setup(world, command=command, bind_protocol=False)
    first = world.service.commit_planning_revision(
        command,
        world.principal,
        admission=admission,
    )
    api.revoke(grant.grant_id, expected_revision=1, command_id="revoke-h1h", reason="stop")

    replay = world.service.commit_planning_revision(
        command,
        world.principal,
        admission=admission,
    )

    assert replay == first
    assert _plan_revision_count(world) == 1
    with pytest.raises(PlanCommitRejected, match="PRINCIPAL_MISMATCH"):
        world.service.commit_planning_revision(
            command,
            dataclasses.replace(world.principal, principal_id="other-manager"),
            admission=admission,
        )


def test_o08_new_action_after_preview_is_detected_by_complete_set_reread(tmp_path) -> None:
    world = _world(tmp_path, key="h1h-o08")
    _, _, admission = _setup(world, dispatchable=True)
    world.store.put_action(
        {
            "mission_id": world.mission.id,
            "action_key": "late-action:v1",
            "action_id": "late-action",
            "version": 1,
            "params_hash": hashlib.sha256(b"{}").hexdigest(),
            "idempotency_key": "late-action:v1",
            "state": "PROPOSED",
            "handoffs": 0,
            "history": [],
            "receipt": None,
        }
    )

    with pytest.raises(PlanCommitRejected, match="OPERATION_SNAPSHOT"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _plan_revision_count(world) == 0


def test_o03_retired_unknown_action_blocks_official_commit_without_revision_or_outbox(
    tmp_path,
) -> None:
    """Mission-wide UNKNOWN remains authoritative outside candidate membership."""

    world = _world(tmp_path, key="h1h-o03-commit")
    # This operation is deliberately absent from the candidate network: it
    # represents a producer occurrence retired by an earlier plan.  H1-H's
    # Store reader is Mission-wide, so retirement cannot hide an unresolved
    # handoff from the official commit gate.
    action = {
            "mission_id": world.mission.id,
            "action_key": "retired-action:v1",
            "action_id": "retired-action",
            "version": 1,
            "params_hash": hashlib.sha256(b"{}").hexdigest(),
            "idempotency_key": "retired-action:v1",
            "state": "UNKNOWN",
            "handoffs": 1,
            "history": [],
            "receipt": None,
    }
    world.store.put_action(action)
    origin = dataclasses.replace(
        envelope(operation_id="retired-action", occurrence="retired-occurrence"),
        mission_id=world.mission.id,
        scope_id=world.principal.scope_id,
    )
    HtnStore(world.store).bind_operation(origin, principal_id=world.principal.principal_id)
    operation_store = PlanningAdmissionStore(world.store)
    binding = operation_store.get_operation_binding(str(origin.operation_occurrence_id))
    assert binding is not None
    link = {
        "operation_id": binding["operation_id"],
        "request_hash": binding["request_hash"],
        "operation_occurrence_id": binding["operation_occurrence_id"],
        "mission_id": binding["mission_id"],
        "envelope_hash": binding["envelope_hash"],
        "principal_id": binding["principal_id"],
        "scope_id": binding["scope_id"],
        "obligation_id": binding["obligation_id"],
        "producer_task_id": "retired-task",
        "producer_htn_occurrence_id": "retired-occurrence",
        "producer_contract_revision": 1,
        "producer_plan_revision": 1,
        "action_key": action["action_key"],
        "action_id": action["action_id"],
        "action_version": action["version"],
        "params_hash": action["params_hash"],
        "idempotency_key": action["idempotency_key"],
        "provenance_receipt_id": "retired-receipt",
        "link_hash": hashlib.sha256(b"retired-link").hexdigest(),
        "link_json": "{}",
    }
    link["link_json"] = canonical_json(link)
    operation_store.put_operation_action_link(link)
    _, _, admission = _setup(world)
    assert admission.operations.effects == (
        ("retired-action", OperationEffect.UNRESOLVED),
    )
    before_events = tuple(world.store.list_events(world.mission.id))
    before_changes = world.store.connection.total_changes

    with pytest.raises(PlanCommitRejected, match="OPERATION_UNRESOLVED"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _plan_revision_count(world) == 0
    # Events are the durable dispatch/outbox boundary for a plan commit.  A
    # rejected commit must emit neither PLAN_REVISION_COMMITTED nor task work.
    assert tuple(world.store.list_events(world.mission.id)) == before_events
    assert world.store.connection.total_changes == before_changes


def test_i06_grant_change_between_preview_and_commit_is_stale(tmp_path) -> None:
    world = _world(tmp_path, key="h1h-i06")
    api, grant, admission = _setup(world)
    api.renew(grant.grant_id, expected_revision=1, command_id="renew-h1h")

    with pytest.raises(PlanCommitRejected, match="REQUEST_BINDING_STALE"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _plan_revision_count(world) == 0


def test_preview_read_set_identity_mismatch_refuses_before_writes(tmp_path) -> None:
    world = _world(tmp_path, key="h1h-preview-read-set")
    _, _, admission = _setup(world)
    admission = dataclasses.replace(admission, preview_read_set_hash="0" * 64)

    with pytest.raises(PlanCommitRejected, match="PREVIEW_IDENTITY_STALE"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _plan_revision_count(world) == 0
