"""Same-Mission authority separation and preview/handoff snapshot invalidation."""
from __future__ import annotations

import sys
from pathlib import Path
import pytest

_HERE = Path(__file__).resolve().parent
for directory in (_HERE, _HERE / 'operation_completion'):
    sys.path.insert(0, str(directory))
from operation_runtime_fixture import materialized_file_publish
from test_h1h_commit_interleaving import _admission_for_active_revision, _second_materialized_revision
from agent_orchestrator.api.approvals import ApprovalApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.plan_commits import PlanCommitRejected
from agent_orchestrator.storage.htn_store import HtnStore


def test_o08_real_handoff_after_preview_invalidates_original_snapshot(tmp_path):
    fixture = materialized_file_publish(tmp_path)
    world = fixture.world
    command = _second_materialized_revision(fixture.plan_command, command_id='o08-next-plan')
    _, _, admission = _admission_for_active_revision(world, command)
    assert not admission.operations.unresolved
    handed, reason = world.service.begin_handoff(fixture.action['action_key'], owner='o08-executor',
        lease_seconds=30, connectors=fixture.connectors, deployment=fixture.deployment)
    assert handed is not None and reason is None
    before = tuple(world.store.iter_events(world.mission.id))
    with pytest.raises(PlanCommitRejected, match='OPERATION_SNAPSHOT_STALE'):
        world.service.commit_planning_revision(command, world.principal, admission=admission)
    assert len(HtnStore(world.store).list_plan_revisions(world.mission.id)) == 1
    assert tuple(world.store.iter_events(world.mission.id)) == before
    assert world.store.get_action(handed['action_key']) == handed
    assert not list(fixture.publish.root.rglob('*'))


def test_a06_same_mission_planning_grant_and_action_approval_are_independent(tmp_path):
    fixture = materialized_file_publish(tmp_path, approve_action=False)
    world = fixture.world
    # Original HTN Plan was committed with a real planning grant in this Mission.
    assert len(HtnStore(world.store).list_plan_revisions(world.mission.id)) == 1
    command = _second_materialized_revision(fixture.plan_command, command_id='a06-next-plan')
    api, grant, admission = _admission_for_active_revision(world, command)
    assert fixture.action['required_approvals'] == 1
    before = tuple(world.store.iter_events(world.mission.id))
    handed, reason = world.service.begin_handoff(fixture.action['action_key'], owner='a06-executor',
        lease_seconds=30, connectors=fixture.connectors, deployment=fixture.deployment)
    assert handed is None and reason == 'not_ready:AWAITING_APPROVAL'
    after_refusal = tuple(world.store.iter_events(world.mission.id))
    assert after_refusal[:len(before)] == before
    assert [e.type for e in after_refusal[len(before):]] == ['ActionHandoffRefused']
    assert world.store.get_action(fixture.action['action_key']) == fixture.action
    ApprovalApi(world.service, Principal('operation-approver'), deployment=fixture.deployment).approve(
        fixture.action['approval_request_id'], nonce='a06-explicit-action-approval')
    api.revoke(grant.grant_id, expected_revision=1, command_id='a06-revoke-plan', reason='stop planning')
    handed, reason = world.service.begin_handoff(fixture.action['action_key'], owner='a06-executor',
        lease_seconds=30, connectors=fixture.connectors, deployment=fixture.deployment)
    assert handed is not None and reason is None
    before = tuple(world.store.iter_events(world.mission.id))
    with pytest.raises(PlanCommitRejected, match='AUTHORIZATION_REQUIRED|REQUEST_BINDING_STALE'):
        world.service.commit_planning_revision(command, world.principal, admission=admission)
    assert len(HtnStore(world.store).list_plan_revisions(world.mission.id)) == 1
    assert tuple(world.store.iter_events(world.mission.id)) == before
    assert not list(fixture.publish.root.rglob('*'))


def test_o04_foreign_tenant_cannot_acquire_planning_authority_for_real_operation(tmp_path):
    from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
    from agent_orchestrator.storage.store import StoreConflict
    fixture = materialized_file_publish(tmp_path)
    world = fixture.world
    outsider = PlanningAuthorizationApi(world.service, tenant_id='foreign-tenant',
                                        principal=Principal('foreign-planner'))
    before = world.store.connection.total_changes
    with pytest.raises(StoreConflict) as rejected:
        outsider.issue(world.mission.id, command_id='o04-foreign-grant')
    assert str(rejected.value) == 'planning mission is not available to this caller'
    assert world.store.connection.total_changes == before
    assert len(HtnStore(world.store).list_plan_revisions(world.mission.id)) == 1
    assert world.store.get_action(fixture.action['action_key']) == fixture.action


def test_o09_real_t0_materialization_rolls_back_link_fault_and_replays_once(tmp_path, monkeypatch):
    import sqlite3
    from agent_orchestrator.orchestrator.commit_service import CommitService
    from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
    from agent_orchestrator.orchestrator.action_commits import ActionCommitError
    put = PlanningAdmissionStore.put_operation_action_link
    materialize = CommitService.materialize_reviewed_operation
    checked = []

    def fail_link(adapter, link):
        assert adapter._store.get_action(link['action_key']) is not None
        # A second real SQLite connection sees neither the uncommitted Action nor
        # its link; a handoff cannot discover a half-materialized operation.
        with sqlite3.connect(adapter._store.path) as reader:
            assert reader.execute('SELECT action_key FROM actions WHERE action_key=?',
                                  (link['action_key'],)).fetchone() is None
        raise RuntimeError('o09 materialization link fault')

    def materialize_with_crash(service, **arguments):
        mission_id = service.store.list_missions()[0].id
        before_actions = service.store.list_actions(mission_id)
        before_events = tuple(service.store.iter_events(mission_id))
        monkeypatch.setattr(PlanningAdmissionStore, 'put_operation_action_link', fail_link)
        with pytest.raises(ActionCommitError, match='o09 materialization link fault'):
            materialize(service, **arguments)
        assert service.store.list_actions(mission_id) == before_actions
        assert tuple(service.store.iter_events(mission_id)) == before_events
        assert not PlanningAdmissionStore(service.store).list_operation_action_links(mission_id)
        monkeypatch.setattr(PlanningAdmissionStore, 'put_operation_action_link', put)
        receipt = materialize(service, **arguments)
        events = tuple(service.store.iter_events(mission_id))
        assert materialize(service, **arguments) == receipt
        assert tuple(service.store.iter_events(mission_id)) == events
        assert len(service.store.list_actions(mission_id)) == 1
        assert len(PlanningAdmissionStore(service.store).list_operation_action_links(mission_id)) == 1
        checked.append(True)
        return receipt

    monkeypatch.setattr(CommitService, 'materialize_reviewed_operation', materialize_with_crash)
    fixture = materialized_file_publish(tmp_path, approve_action=False)
    assert checked == [True] and fixture.action['handoffs'] == 0
    assert not list(fixture.publish.root.rglob('*'))


def test_a06_zero_action_approvals_and_observed_method_gate_cannot_replace_grant(tmp_path, monkeypatch):
    import dataclasses
    import test_plan_commits as plan_fixture
    from agent_orchestrator.contracts.evidence_state import TruthValue
    from agent_orchestrator.contracts.htn import parse_conditions
    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
    from test_h1h_commit_guard import _setup
    from test_h1h_commit_interleaving import _install_proposed_linked_action
    from htn_world import ref, param
    original_env, original_method = plan_fixture._env, plan_fixture._outer

    def observed_env(mission_id):
        env = original_env(mission_id)
        env.register_predicate('plan.permission_observed')
        env.say('plan.permission_observed', {'subject': 'alpha'}, TruthValue.TRUE)
        return env

    def gated_method():
        return dataclasses.replace(original_method(), applicable_when=parse_conditions([
            {'op': 'predicate', 'predicate_ref': ref('plan.permission_observed').to_json(),
             'arguments': {'subject': param('subject')}}], 'applicable_when'))

    monkeypatch.setattr(plan_fixture, '_env', observed_env)
    monkeypatch.setattr(plan_fixture, '_outer', gated_method)
    world = plan_fixture._world(tmp_path, key='a06-zero-approvals')
    report = assess_method(world.binding, world.contract, world.env.snapshot(),
                           world.env.capabilities(), registry=world.env.predicates)
    assert report.authorization.allowed is True
    action, _ = _install_proposed_linked_action(world, tmp_path)
    assert action['required_approvals'] == 0
    _, _, admission = _setup(world)
    world.store.connection.execute('DELETE FROM planning_request_authority_bindings WHERE request_id=?',
                                   (admission.request_id,))
    assert PlanningAdmissionStore(world.store).get_request_binding(admission.request_id) is None
    before = world.store.connection.total_changes
    with pytest.raises(PlanCommitRejected, match='AUTHORIZATION_REQUIRED'):
        world.service.commit_planning_revision(world.command, world.principal, admission=admission)
    assert world.store.connection.total_changes == before
    assert not HtnStore(world.store).list_plan_revisions(world.mission.id)
    assert world.store.get_action(action['action_key']) == action
