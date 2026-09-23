"""Item 6 seam: production installer + four real consumers on the durable tick.

Part A drives the real ``Orchestrator`` startup with ``install_assurance`` as its
deployment assembly (native root, fixed principal authority, factory selector,
activation reconciliation, NOTIFY transport). Part B replaces the fixture's
single-consumer pump with the real ``AssuranceTick`` over REVIEW / VALIDITY /
CLOSEOUT / NOTIFY on the assured fixture Mission: leaf accept -> closeout
NOT_READY -> MISSION_FINAL -> root GoalResolution -> closeout re-evaluation ->
pre-Scope METHOD_PLAN official record -> certificate expiry -> NOTIFY.
Fixture routing/ACL/lease; not a real model, Host or UI; the final writer chain is
final-writer-seam (item 7).
"""
from _assured_fixture import (EVIDENCE, SDK, AssuredRuntime, count, refused, requirements_ref,  # noqa: F401
                              source_sha256, PRINCIPAL, TENANT)
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from htn_world import method, param, step, task_binding
from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import AssuranceError, decode
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts import Budget, Task
from agent_orchestrator.contracts.htn import TaskForm
from agent_orchestrator.contracts.resolution import ReviewVerdict
from agent_orchestrator.contracts.state_machines import TaskStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_assembly import AssuranceDeploymentPorts, install_assurance
from agent_orchestrator.orchestrator.assurance_consumers import (
    CLOSEOUT_EVENT, NOTIFICATION_EVENT, NOTIFIED_EVENT, VALIDITY_CHECKED_EVENT,
    AssuranceCloseoutConsumer, AssuranceNotifyConsumer, AssuranceValidityConsumer)
from agent_orchestrator.orchestrator.assurance_tick import AssuranceTick
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.plan_commits import PlanPrincipal
from agent_orchestrator.orchestrator.root_review import RootReviewCoordinator, RootReviewStatus
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

CONTENT_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
                 'evidence_ids': [], 'reason': 'fixture response', 'limitations': []}], 'findings': []}
FINAL_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
               'evidence_ids': [], 'reason': 'fixture root review', 'limitations': []}], 'findings': []}
METHOD_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
                'evidence_ids': [], 'reason': 'fixture method review', 'limitations': []}], 'findings': []}


def events_of(store, mission_id, kind):
    return [e for e in store.list_events(mission_id, limit=10_000) if e.type == kind]


def pending(store, mission_id):
    return [dict(r) for r in store.connection.execute(
        'SELECT consumer,work_key,state,tries,wait_reason FROM assurance_pending_work WHERE mission_id=? ORDER BY consumer,work_key',
        (mission_id,)).fetchall()]


def closeout_row(store, mission_id):
    row = store.connection.execute('SELECT mission_id,resolution_id,state,row_version,check_body_hash,last_receipt_id '
                                   'FROM assurance_closeouts WHERE mission_id=?', (mission_id,)).fetchone()
    return None if row is None else dict(row)


async def drain(tick, limit=12):
    rounds = 0
    while rounds < limit and await tick.tick():
        rounds += 1
    return rounds


# ------------------------------------------------------------------ part A
async def production_install(report):
    with TemporaryDirectory(prefix='assurance-four-consumer-install-') as temp:
        base = Path(temp).resolve()
        cfg = OrchestratorConfig(evidence_root=base / 'root')
        principal = Principal('fixture-current-user')
        provider = RoleScriptedProvider({})
        sent, installed = [], {}

        def root_setup(orch):
            orch.commit.install_assurance_root(principal=principal, tenant_id='tenant', command_id='install')

        def ports():
            return AssuranceDeploymentPorts(
                tenant_id='tenant', principal=principal,
                select_profile=lambda spec: AssurancePolicy() if spec.idempotency_key.startswith('assured') else None,
                notify_transport=sent.append)

        def assembly(orch):
            installed['value'] = install_assurance(orch, ports())

        async with Orchestrator(cfg, provider, assurance_root_setup=root_setup, startup_assembly=assembly) as orch:
            inst = installed['value']
            store, commit = orch.store, orch.commit
            assert orch._assurance_tick is inst.tick and commit._assurance_factory is inst.factory
            assert orch._assurance_local_checks is inst.local_checks and orch._assurance_reviews is inst.review_runtime
            assert commit._assurance_read_authority == inst.authority.read and commit._assurance_validity is inst.validity
            assert set(inst.consumers) == {'REVIEW', 'VALIDITY', 'CLOSEOUT', 'NOTIFY'}
            assert inst.startup == {'missions': 0, 'cursors_rebuilt': [], 'expiry_events_emitted': 0, 'pending_work': {}}, inst.startup
            assert refused(lambda: install_assurance(orch, ports()), {'ASSURANCE_ALREADY_INSTALLED'})
            spec = MissionSpec(goal='assured fixture', success_criteria=('the answer file is written', 'it names the fixture'),
                               tenant_id='tenant', idempotency_key='assured-1', orchestration_semantics_version='hierarchical',
                               planning_protocol_version='planning-decision-v1')
            assured, created = commit.create_mission(spec)
            assert created and AssuranceStore(store).lane(assured.id) == 'ASSURANCE_1_1'
            req = HtnStore(store).get_requirements_revision(assured.id, 1)
            assert str(req.revision_id) == f'req-{assured.id}-1' and [c.criterion_id for c in req.criteria] == ['c-user-1', 'c-user-2']
            assert req.authority_subject == principal.principal_id and all(str(c.origin) == 'USER_EXPLICIT' for c in req.criteria)
            cursors = {r[0]: r[1] for r in store.connection.execute(
                'SELECT consumer,last_event_seq FROM assurance_event_cursors WHERE mission_id=?', (assured.id,))}
            activation = events_of(store, assured.id, 'AssuranceProfileActivated')[0]
            assert set(cursors) == {'REVIEW', 'VALIDITY', 'CLOSEOUT', 'NOTIFY'} and set(cursors.values()) == {activation.seq}
            assert not pending(store, assured.id)
            again, created_again = commit.create_mission(spec)
            assert again.id == assured.id and not created_again
            legacy, _ = commit.create_mission(MissionSpec(goal='legacy', success_criteria=('c',), tenant_id='tenant', idempotency_key='legacy-1'))
            unselected, _ = commit.create_mission(MissionSpec(goal='v1 unselected', success_criteria=('c',), tenant_id='tenant',
                idempotency_key='completion-1', orchestration_semantics_version='hierarchical', planning_protocol_version='planning-decision-v1'))
            lanes = {'assured': AssuranceStore(store).lane(assured.id), 'legacy': AssuranceStore(store).lane(legacy.id),
                     'v1_unselected': AssuranceStore(store).lane(unselected.id)}
            assert lanes == {'assured': 'ASSURANCE_1_1', 'legacy': 'LEGACY', 'v1_unselected': 'COMPLETION_V1'}, lanes
            # Fixed principal / current ACL through the original facade hook.
            ref = AssuranceRef('requirements', Pin(str(req.revision_id), 1, req.content_hash()))
            permission = commit._assurance_read_authority(principal, 'tenant', assured.id, ref, 'DISCLOSE')
            assert permission.access.channel == 'ACCESS' and permission.policy.channel == 'POLICY'
            assert permission.not_after_ms > int(store.now * 1000)
            same = commit._assurance_read_authority(principal, 'tenant', assured.id, ref, 'DISCLOSE')
            assert same.access == permission.access and same.policy == permission.policy
            other = commit._assurance_read_authority(principal, 'tenant', assured.id, ref, 'CONTEXT')
            assert other.access != permission.access and other.policy != permission.policy
            assert refused(lambda: commit._assurance_read_authority(Principal('someone-else'), 'tenant', assured.id, ref, 'DISCLOSE'),
                           {'ROOT_READ_NOT_AUTHORIZED'})
            assert refused(lambda: commit._assurance_read_authority(principal, 'other-tenant', assured.id, ref, 'DISCLOSE'),
                           {'ROOT_READ_NOT_AUTHORIZED'})
            assert refused(lambda: commit._assurance_read_authority(principal, 'tenant', assured.id, ref, 'BOGUS'),
                           {'CURRENT_READ_AUTHORITY_REQUIRED'})
            # Idle tick: ingestion only, nothing claimed.
            idle = await drain(orch._assurance_tick)
            assert not pending(store, assured.id) and not orch._assurance_tick.has_pending()
            # NOTIFY: the (future) final writer's request, delivered at least once.
            final = events_of(store, assured.id, 'MissionCreated')[0]
            commit._emit(NOTIFICATION_EVENT, assured.id, key=final.id,
                         payload={'final_event_id': final.id, 'state_version': assured.version, 'final_event_type': final.type})
            rounds = await drain(orch._assurance_tick)
            assert sent == [{'mission_id': assured.id, 'event_id': final.id, 'state_version': assured.version}], sent
            receipt = store.get_receipt('assurance-notified:notify:' + assured.id + ':' + final.id)
            assert receipt is not None and receipt['delivered_payload'] == sent[0]
            assert len(events_of(store, assured.id, NOTIFIED_EVENT)) == 1
            work = pending(store, assured.id)
            assert [(w['consumer'], w['state']) for w in work] == [('NOTIFY', 'DONE')], work
            await drain(orch._assurance_tick)
            assert len(sent) == 1 and not orch._assurance_tick.has_pending()
            # Restart reconciliation: a lost cursor is rebuilt at 0 and replays by stable keys.
            store.connection.execute("DELETE FROM assurance_event_cursors WHERE mission_id=? AND consumer='NOTIFY'", (assured.id,))
            from agent_orchestrator.orchestrator.assurance_assembly import reconcile_startup
            summary = reconcile_startup(orch, tenant_id='tenant', root_incarnation_id=inst.root_incarnation_id)
            assert summary['cursors_rebuilt'] == [assured.id + ':NOTIFY'] and summary['missions'] == 1, summary
            await drain(orch._assurance_tick)
            assert len(sent) == 1 and [(w['consumer'], w['state']) for w in pending(store, assured.id)] == [('NOTIFY', 'DONE')]
            report['production_install'] = {
                'startup': inst.startup, 'lanes': lanes, 'requirements': {'id': str(req.revision_id), 'criteria': [c.criterion_id for c in req.criteria]},
                'cursor_seq': cursors, 'idle_rounds': idle, 'notify_rounds': rounds, 'sent': sent,
                'cursor_rebuild': summary, 'second_install': 'ASSURANCE_ALREADY_INSTALLED',
                'authority': {'access_key': permission.access.key, 'policy_key': permission.policy.key}}


# ------------------------------------------------------------------ part B
def binding_of(rt, review_key):
    row = rt.store.connection.execute('SELECT binding_json FROM assurance_review_bindings WHERE mission_id=? AND review_key=?',
                                      (rt.mission.id, review_key)).fetchone()
    return None if row is None else decode(row['binding_json'])


def invocations(rt, prefix):
    return rt.store.connection.execute(
        "SELECT review_key FROM assurance_review_invocations WHERE mission_id=? AND review_key LIKE ? AND ordinal=1",
        (rt.mission.id, prefix + '%')).fetchall()


async def method_plan_pre_scope(rt, report):
    """METHOD_PLAN before any Scope: identity scope 'mission' end to end (was NOT_REACHED)."""
    store, commit, mission, world = rt.store, rt.commit, rt.mission, rt.world
    htn = HtnStore(store)
    world.env.register_type('seam.goal', form=TaskForm.COMPOUND, parameters=(('subject', 'string'),),
                            criteria=('criterion-report',), domain='plan')
    planning = task_binding(world.env, 'seam.goal', task_id='task-seam-plan', obligation='obl-root', parameters={'subject': 'alpha'})
    with store.transaction():
        htn.put_task_semantics(mission.id, planning)
        store.insert_task(Task(id='task-seam-plan', mission_id=mission.id, parent_task_ids=(), dependency_ids=(),
                               goal='seam compound planning subject', rationale='fixture planning subject',
                               success_criteria=('criterion-report',), verification_policy=('critic_review',), allowed_tools=(),
                               budget=Budget(max_tokens=10_000), priority=1.0, status=TaskStatus.READY, version=1,
                               root_goal=mission.goal, created_at=store.now, ready_at=store.now), ordinal=2)
    contract = method('seam.method', 'seam.goal', parameter_schema='seam.goal.params',
                      steps=(step('leaf', 'plan.leaf', TaskForm.PRIMITIVE, {'subject': param('subject')}, capabilities=('plan.read',)),),
                      links=(('criterion-report', 'leaf', None),), finalizer='leaf')
    receipt = world.env.admit(contract)
    assert receipt.admitted, receipt.problems
    htn.register_method(contract, world.env.registry.registration(contract.method_ref()))
    ref = contract.method_ref()
    pin = Pin(ref.method_id, int(ref.version), ref.content_hash)
    binding = htn.task_semantics_of(mission.id, 'task-seam-plan')
    subject_ref = AssuranceRef('task', Pin('task-seam-plan', int(binding.contract_revision), binding.content_hash()))
    req = htn.get_requirements_revision(mission.id, 1)
    commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id, command_id='fixture-method-plan-policy',
        principal=Principal('fixture-authenticated-user'), requirements_ref=requirements_ref(req),
        planning_subject=subject_ref, purpose='METHOD_PLAN', candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
    with store.transaction():
        rt.runner.ensure_method_plan(mission, task_id='task-seam-plan', method_ref=pin, producer_agent_ids=('fixture-planner',))
    review_key = invocations(rt, 'assurance-method-plan:')[0][0]
    bound = binding_of(rt, review_key)
    assert bound['subject']['completion_scope_ref'] is None and bound['subject']['purpose'] == 'METHOD_PLAN'
    intent = rt.invocation_intent(review_key)
    rt.provider.script.append(json.dumps(METHOD_REPLY))
    calls_before = rt.provider.calls
    dispatched = await rt.orch._dispatch(intent)
    assert dispatched is True, rt.notes[-5:]
    await rt.drive_review(review_key)
    assert rt.provider.calls == calls_before + 1
    package_id = bound['package_ref']['id']
    with store.read_view():
        record = htn.official_review_record(package_id)
    assert record is not None and record.verdict is ReviewVerdict.ACCEPT and str(record.purpose) == 'METHOD_PLAN', record
    # The official import binds the record on the side table (no use certificate
    # is issued by an import; a PLAN use of this record is the admission's job).
    side = store.connection.execute('SELECT record_id,review_key,import_receipt_id FROM assurance_review_record_bindings '
                                    'WHERE record_id=?', (str(record.record_id),)).fetchone()
    assert side is not None and side['review_key'] == review_key, side
    gateway_binding = [r for r in rt.gateway.calls if r.get('review_key') == review_key]
    work = [w for w in pending(store, mission.id) if w['consumer'] == 'REVIEW' and review_key in w['work_key']]
    assert work and all(w['state'] == 'DONE' for w in work), work
    report['method_plan_pre_scope'] = {'review_key': review_key, 'scope_present': False, 'identity_scope': 'mission',
                                       'official_record': str(record.record_id), 'verdict': str(record.verdict),
                                       'record_binding_receipt': side['import_receipt_id'], 'dispatched': dispatched,
                                       'evidence_tool_calls_bound_to_review': len(gateway_binding)}


async def four_consumers(root, report):
    async with AssuredRuntime(root, [CONTENT_REPLY], content_only=True) as rt:
        store, commit, mission = rt.store, rt.commit, rt.mission
        gate = commit._assurance_root_gate
        root_id = gate.require_execution().root_incarnation_id
        sent = []
        consumers = {'REVIEW': rt.consumer,
                     'VALIDITY': AssuranceValidityConsumer(commit, tenant_id=TENANT),
                     'CLOSEOUT': AssuranceCloseoutConsumer(commit, tenant_id=TENANT),
                     'NOTIFY': AssuranceNotifyConsumer(commit, tenant_id=TENANT, transport=sent.append)}
        tick = AssuranceTick(rt.orch, consumers=consumers, root_incarnation_id=root_id,
                             require_execution_root=gate.require_execution, tenant_id=TENANT)
        rt.pump = tick
        rt.orch._assurance_tick = tick
        # 1. Leaf: official TASK_CONTENT review through the real tick, then the original acceptance.
        verdict, record = await rt.run_critic()
        assert verdict.passed
        rt.record_critic_layer(record)
        # The fixture Worker never ran in the runtime: its usage is imported as
        # UNKNOWN (as production does when the price is unknown), so this Mission's
        # closeout can honestly reach DRAINING(USAGE_UNKNOWN) but never READY here.
        rt.settle_fixture_worker()
        accepted = rt.accept_now()
        assert accepted.accepted_result_id == rt.stored.envelope.id
        await drain(tick)
        closeouts = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)]
        assert closeouts and closeouts[-1]['state'] == 'NOT_READY' and closeouts[-1]['reasons'] == ['ROOT_RESOLUTION_MISSING'], closeouts
        assert closeout_row(store, mission.id) is None  # no resolution row to reference yet
        validity_after_accept = [e.payload for e in events_of(store, mission.id, VALIDITY_CHECKED_EVENT)]
        report['after_leaf_accept'] = {'closeout': closeouts[-1], 'validity_checks': validity_after_accept,
                                       'pending': pending(store, mission.id)}
        # 2. MISSION_FINAL through the original root cut, then the root GoalResolution.
        req = HtnStore(store).get_requirements_revision(mission.id, 1)
        commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id, command_id='fixture-mission-final-policy',
            principal=Principal('fixture-authenticated-user'), requirements_ref=requirements_ref(req),
            completion_scope=rt.scope_ref, purpose='MISSION_FINAL', candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
        dispatch = HierarchicalDispatch(store, commit)
        coordinator = RootReviewCoordinator(store, commit, dispatch, scope_id='mission', issued_by='runner-fixture', max_cuts_per_revision=2)
        package = coordinator.cut(mission.id, now_ms=int(store.now * 1000))
        rt.provider.script.append(json.dumps(FINAL_REPLY))
        assert await rt.orch._ask_root_reviewer(mission, coordinator, package) is True, rt.notes[-5:]
        review_key = 'assurance-mission-final:' + mission.id + ':' + str(package.package_id)
        keys = [r[0] for r in invocations(rt, 'assurance-mission-final:')]
        assert keys, keys
        review_key = keys[0]
        await rt.drive_review(review_key)
        assert coordinator.state(mission.id).status is RootReviewStatus.READY, coordinator.state(mission.id)
        # Item 7: the production trigger forms the assured root resolution under the current
        # MISSION_FINAL UseCertificate (final-writer-seam covers judge -> closeout -> final writer).
        outcome = dispatch.attempt_root_resolution(mission.id, principal=PlanPrincipal('fixture-authenticated-user'),
                                                   command_id='fixture-root-resolution')
        assert outcome.committed, outcome
        await drain(tick)
        closeouts = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)]
        assert closeouts[-1]['state'] == 'NOT_READY' and closeouts[-1]['reasons'] == ['MISSION_JUDGMENT_MISSING'], closeouts[-1]
        row = closeout_row(store, mission.id)
        assert row and row['state'] == 'NOT_READY' and row['row_version'] == 1, row
        with store.read_view():
            final_record = HtnStore(store).official_review_record(str(package.package_id))
        resolution = HtnStore(store).get_goal_resolution(outcome.resolution_id)
        report['after_mission_final'] = {
            'root_review_state': str(coordinator.state(mission.id).status), 'official_record': str(final_record.record_id),
            'record_verdict': str(final_record.verdict),
            'record_criteria_projection': {str(c.criterion_id): str(c.verdict) for c in final_record.criteria},
            'root_resolution': {'production_trigger': 'committed', 'resolution_id': outcome.resolution_id,
                                'criteria': {str(c.criterion_id): str(c.verdict) for c in resolution.criteria}},
            'closeout_states': [c['state'] for c in closeouts], 'closeout_row': row}
        # 3. Pre-Scope METHOD_PLAN consumed on the same tick (item 6 scope).
        await method_plan_pre_scope(rt, report)
        # 4. Expiry: the VALIDITY worker is the timing owner; the tick emits the due
        #    events from the certificate table and VALIDITY records the observation.
        certificates_before = count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=? "
                                    "AND json_extract(certificate_json,'$.decision')='USABLE' AND not_after_ms IS NOT NULL", mission.id)
        shift = 61.0
        store._clock = lambda: time.time() + shift
        rounds = await drain(tick)
        due = events_of(store, mission.id, 'AssuranceUseExpiryDue')
        checks = [e.payload for e in events_of(store, mission.id, VALIDITY_CHECKED_EVENT)]
        expired = [c for c in checks if 'EXPIRED' in c['reasons']]
        assert due and expired, (len(due), checks[-3:])
        assert {c['certificate_id'] for c in expired} >= {e.payload['certificate_id'] for e in due}, 'every due certificate observed'
        validity_work = [w for w in pending(store, mission.id) if w['consumer'] == 'VALIDITY']
        assert validity_work and all(w['state'] == 'DONE' for w in validity_work), validity_work
        closeout_after_expiry = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)][-1]
        report['expiry'] = {'usable_certificates_with_deadline': certificates_before, 'due_events': len(due),
                            'expired_observations': len(expired), 'rounds': rounds,
                            'sample': expired[-1], 'closeout_after_expiry': closeout_after_expiry['state']}
        # 5. NOTIFY on the fixture Mission (request emitted by hand: the final writer is item 7).
        final = events_of(store, mission.id, 'MissionCreated')[0]
        commit._emit(NOTIFICATION_EVENT, mission.id, key=final.id,
                     payload={'final_event_id': final.id, 'state_version': 1, 'final_event_type': final.type})
        await drain(tick)
        assert sent == [{'mission_id': mission.id, 'event_id': final.id, 'state_version': 1}], sent
        await drain(tick)
        assert len(sent) == 1
        report['notify'] = {'sent': sent, 'receipt': store.get_receipt('assurance-notified:notify:' + mission.id + ':' + final.id) is not None}
        report['work_table'] = pending(store, mission.id)
        assert not tick.has_pending(), pending(store, mission.id)
        report['provider_calls'] = rt.provider.calls
        store.close()


async def main():
    report = {'scope': 'Item 6: install_assurance on the real Orchestrator startup (native root, fixed-principal current '
                       'authority, factory selector -> ASSURANCE_1_1 / COMPLETION_V1 / LEGACY lanes, honest activation '
                       'inventory, startup cursor rebuild, NOTIFY at-least-once); real AssuranceTick with REVIEW + VALIDITY + '
                       'CLOSEOUT + NOTIFY on the assured fixture: leaf accept -> closeout NOT_READY -> MISSION_FINAL official ACCEPT -> '
                       'root resolution formed by the production trigger under the current MISSION_FINAL use (item 7) '
                       '-> pre-Scope METHOD_PLAN official record (scope "mission") '
                       '-> certificate expiry observed by VALIDITY -> NOTIFY. Fixture routing/ACL/lease; not a real model, '
                       'Host or UI; no final writer (item 7), no recovery/pin reconciliation (item 8).'}
    await production_install(report)
    with TemporaryDirectory(prefix='assurance-four-consumer-') as temp:
        await four_consumers(Path(temp).resolve(), report)
    report['status'] = 'PASS'
    report['source_sha256'] = source_sha256(['orchestrator/assurance_assembly.py', 'orchestrator/assurance_consumers.py',
        'orchestrator/assurance_factory.py', 'orchestrator/assurance_tick.py', 'orchestrator/assurance_review_consumer.py',
        'orchestrator/assurance_review_import.py', 'orchestrator/assurance_review_handoff.py',
        'verification/reviewer_evidence_tools.py', 'assurance/expiry.py'])
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    out = EVIDENCE / ('four-consumer-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out), 'lanes': report['production_install']['lanes'],
                      'closeout': report['after_mission_final']['closeout_states'],
                      'method_plan': report['method_plan_pre_scope']['verdict'],
                      'expiry': report['expiry']['expired_observations']}, ensure_ascii=False))


asyncio.run(main())
