"""Other-purpose builders on the unified round transport (BW03), fixture Mission.

Exercised end to end: MISSION_FINAL from the original root coordinator's cut
(`RootReviewCoordinator.cut` -> `Orchestrator._ask_root_reviewer` assured branch
-> purpose policy -> invocation -> actual turn -> official record) and
METHOD_PLAN from the runtime entry the method admission hook calls, with its
counter-cases. COMPOSITION / ACTION_PROPOSAL / OPERATION_OUTCOME builders are
code only here (their original entries need compound / operation fixtures).
"""
from _assured_fixture import (EVIDENCE, SDK, AssuredRuntime, count, refused, requirements_ref,  # noqa: F401
                              source_sha256, PRINCIPAL, TENANT)
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from htn_world import method, param, step, task_binding
from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import decode
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts import Budget, Task
from agent_orchestrator.contracts.htn import TaskForm
from agent_orchestrator.contracts.state_machines import TaskStatus
from agent_orchestrator.contracts.resolution import ReviewVerdict
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_purpose_reviews import policy_domain_hash
from agent_orchestrator.orchestrator.assurance_review_import import read_official_review_binding_locked
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.root_review import RootReviewCoordinator, RootReviewStatus
from agent_orchestrator.storage.htn_store import HtnStore

CONTENT_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
                 'evidence_ids': [], 'reason': 'fixture response', 'limitations': []}], 'findings': []}
FINAL_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [
    {'criterion_id': 'criterion-report', 'verdict': 'PASS', 'evidence_ids': [], 'reason': 'fixture root review', 'limitations': []}],
    'findings': []}
METHOD_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
                'evidence_ids': [], 'reason': 'fixture method review', 'limitations': []}], 'findings': []}


def binding_of(rt, review_key):
    row = rt.store.connection.execute('SELECT binding_json FROM assurance_review_bindings WHERE mission_id=? AND review_key=?',
                                      (rt.mission.id, review_key)).fetchone()
    return None if row is None else decode(row['binding_json'])


def invocations(rt, prefix):
    return rt.store.connection.execute(
        "SELECT review_key FROM assurance_review_invocations WHERE mission_id=? AND review_key LIKE ? AND ordinal=1",
        (rt.mission.id, prefix + '%')).fetchall()


async def mission_final(rt, report):
    store, commit, mission = rt.store, rt.commit, rt.mission
    dispatch = HierarchicalDispatch(store, commit)
    coordinator = RootReviewCoordinator(store, commit, dispatch, scope_id='mission', issued_by='runner-fixture', max_cuts_per_revision=2)
    state = coordinator.state(mission.id)
    assert state.status is RootReviewStatus.CUT_REQUIRED, state
    witnesses_sql = "SELECT COUNT(*) FROM validity_witnesses WHERE mission_id=? AND purpose='ACCEPT'"
    before = count(store, witnesses_sql, mission.id)
    package = coordinator.cut(mission.id, now_ms=int(store.now * 1000))
    assert str(package.purpose) == 'MISSION_FINAL' and int(package.binding.requirements_revision) == 1
    assert {c.criterion_id for c in package.criteria} == {'criterion-report'}
    # The original cut still mints its legacy ACCEPT witness. That row is history,
    # not a licence: nothing here consumes it (BW07 terminal writer is item 7).
    report['legacy_cut_witness'] = {'minted': count(store, witnesses_sql, mission.id) - before, 'consumed_here': False}
    # 1. No purpose policy approved -> the assured branch refuses, writes no binding.
    notes_before = len(rt.notes)
    asked = await rt.orch._ask_root_reviewer(mission, coordinator, package)
    assert asked is False and 'CHECK_POLICY_UNRESOLVED' in ' '.join(rt.notes[notes_before:]), rt.notes[notes_before:]
    assert not invocations(rt, 'assurance-mission-final:')
    assert count(store, "SELECT COUNT(*) FROM assurance_review_bindings WHERE mission_id=? AND review_key LIKE 'assurance-mission-final:%'", mission.id) == 0
    report['mission_final_no_policy'] = 'CHECK_POLICY_UNRESOLVED'
    # 2. The TASK_CONTENT policy on the same Scope does not license MISSION_FINAL:
    #    the purpose domain differs from the Scope hash.
    scope_hash = rt.scope_ref.pin.content_hash
    final_domain = policy_domain_hash('MISSION_FINAL', scope_hash=scope_hash, task_hash='0' * 64)
    assert final_domain != scope_hash
    req = HtnStore(store).get_requirements_revision(mission.id, 1)
    policy_ref = commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id,
        command_id='fixture-mission-final-policy', principal=Principal('fixture-authenticated-user'),
        requirements_ref=requirements_ref(req), completion_scope=rt.scope_ref, purpose='MISSION_FINAL',
        candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
    row = store.connection.execute('SELECT scope_hash FROM assurance_criterion_policies WHERE policy_id=?', (policy_ref.pin.id,)).fetchone()
    assert row['scope_hash'] == final_domain
    # 3. The assured branch of the original root reviewer entry.
    rt.provider.script.append(json.dumps(FINAL_REPLY, ensure_ascii=False))
    asked = await rt.orch._ask_root_reviewer(mission, coordinator, package)
    assert asked is True, rt.notes
    keys = invocations(rt, 'assurance-mission-final:')
    assert len(keys) == 1, keys
    review_key = keys[0][0]
    bound = binding_of(rt, review_key)
    subject = bound['subject']
    assert subject['purpose'] == 'MISSION_FINAL' and subject['target']['kind'] == 'task'
    assert subject['target']['pin']['id'] == str(package.binding.subject_ref.id)
    assert subject['completion_scope_ref']['id'] == rt.scope_ref.pin.id
    assert subject['output_manifest_hash'] is not None and subject['method_instance_ref'] is None
    assert bound['criterion_policy_ref']['pin']['id'] == policy_ref.pin.id
    assert bound['criterion_ids'] == ['criterion-report']
    intent = rt.invocation_intent(review_key)
    assert intent.kind == 'plan' and intent.config.get('assurance_protocol') == 'assurance-exec-v1.1'
    # Legacy root reviewer intent must not exist for an assured Mission.
    assert store.get_intent_for_subject(f'{mission.id}:root-review:{package.package_id}:1') is None
    # Idempotent: asking again replays the same invocation and, as on the legacy
    # path, reports no progress (a package already out for review is not a step).
    asked = await rt.orch._ask_root_reviewer(mission, coordinator, package)
    assert asked is False and len(invocations(rt, 'assurance-mission-final:')) == 1
    assert coordinator.state(mission.id).status is RootReviewStatus.AWAITING_REVIEW
    # 4. Actual turn -> collection -> REVIEW consumer -> official record.
    calls_before = rt.provider.calls
    await rt.drive_review(review_key)
    assert rt.provider.calls == calls_before + 1
    assert not rt.pump.rejections, rt.pump.rejections
    with store.read_view():
        record = HtnStore(store).official_review_record(str(package.package_id))
        assert record is not None and record.verdict is ReviewVerdict.ACCEPT, record
        assert str(record.purpose) == 'MISSION_FINAL'
        read_official_review_binding_locked(commit, TENANT, record)
    state = coordinator.state(mission.id)
    assert state.status is RootReviewStatus.READY, state
    report['mission_final'] = {
        'package_id': str(package.package_id), 'review_key': review_key, 'record_id': str(record.record_id),
        'policy_domain': final_domain, 'model_verdict': str(record.verdict), 'root_state': str(state.status),
        'criteria': {o.criterion_id: (o.verdict.value, tuple(o.limitations)) for o in record.criteria},
        'invocations': 1, 'intent_kind': intent.kind, 'legacy_root_intent': False}


async def method_plan(rt, report):
    store, commit, mission, world = rt.store, rt.commit, rt.mission, rt.world
    htn = HtnStore(store)
    runner = rt.runner
    outer = world.contract.method_ref()
    outer_pin = Pin(outer.method_id, int(outer.version), outer.content_hash)

    def ensure(task_id, pin, producers=('fixture-planner',)):
        def call():
            with store.transaction():  # production calls this inside the admission UoW
                return runner.ensure_method_plan(mission, task_id=task_id, method_ref=pin, producer_agent_ids=producers)
        return call

    # 1. plan.outer serves plan.goal; task-root covers c-root which no requirements criterion names.
    report['method_plan_uncovered'] = refused(ensure('task-root', outer_pin), {'SOURCE_UNAVAILABLE'})
    # 2. A method for another goal is not a plan for this subject.
    report['method_plan_goal_mismatch'] = refused(ensure('task-completion-root', outer_pin), {'SUBJECT_BINDING_INVALID'})
    # 3. Only a compound goal is refined by a method (§6.2): the planning subject is a
    #    compound Task covering criterion-report, registered as plan admission would
    #    (semantic binding + Task row), and a method for that goal admitted and
    #    registered through the real registry.
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
    # 3a. No provable author.
    report['method_plan_no_author'] = refused(ensure('task-seam-plan', pin, producers=('',)), {'SOURCE_UNAVAILABLE'})
    # 3b. The Scope's TASK_CONTENT policy does not license a planning judgement.
    report['method_plan_no_policy'] = refused(ensure('task-seam-plan', pin), {'CHECK_POLICY_UNRESOLVED'})
    assert not invocations(rt, 'assurance-method-plan:')
    # 3c. Approve on the planning subject (exact Task contract), never on a Scope.
    binding = htn.task_semantics_of(mission.id, 'task-seam-plan')
    subject_ref = AssuranceRef('task', Pin('task-seam-plan', int(binding.contract_revision), binding.content_hash()))
    req = htn.get_requirements_revision(mission.id, 1)
    policy_ref = commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id,
        command_id='fixture-method-plan-policy', principal=Principal('fixture-authenticated-user'),
        requirements_ref=requirements_ref(req), planning_subject=subject_ref, purpose='METHOD_PLAN',
        candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
    row = store.connection.execute('SELECT scope_hash FROM assurance_criterion_policies WHERE policy_id=?', (policy_ref.pin.id,)).fetchone()
    assert row['scope_hash'] == binding.content_hash() == policy_domain_hash('METHOD_PLAN', scope_hash=rt.scope_ref.pin.content_hash, task_hash=binding.content_hash())
    invocation = ensure('task-seam-plan', pin)()
    keys = invocations(rt, 'assurance-method-plan:')
    assert len(keys) == 1
    review_key = keys[0][0]
    bound = binding_of(rt, review_key)
    subject = bound['subject']
    assert subject['purpose'] == 'METHOD_PLAN' and subject['target'] == AssuranceRef('method', pin).to_json()
    assert subject['owner_task_ref']['id'] == 'task-seam-plan' and subject['output_manifest_hash'] is None
    assert subject['completion_scope_ref'] is None and subject['occurrence_id'] is None, subject
    assert bound['criterion_policy_ref']['pin']['id'] == policy_ref.pin.id and bound['criterion_ids'] == ['criterion-report']
    assert bound['producer_agent_ids'] == ['fixture-planner']
    intent = rt.invocation_intent(review_key)
    assert intent.kind == 'plan' and intent.state == 'PENDING'
    # Replay returns the same invocation; a different package for the same key cannot appear.
    again = ensure('task-seam-plan', pin)()
    assert again.to_json() == invocation.to_json() and len(invocations(rt, 'assurance-method-plan:')) == 1
    # 3d. Pre-Scope consumption (item 6): the handoff, the evidence tools and the
    #     REVIEW consumer bind the METHOD_PLAN use identity to scope "mission"
    #     (the purpose builder's own scope_id), so the invocation is dispatched,
    #     the actual turn is collected and the official record is imported.
    notes_before, calls_before = len(rt.notes), rt.provider.calls
    rt.provider.script.append(json.dumps(METHOD_REPLY))
    dispatched = await rt.orch._dispatch(intent)
    assert dispatched is True, rt.notes[notes_before:]
    await rt.drive_review(review_key)
    assert rt.provider.calls == calls_before + 1
    with store.read_view():
        record = htn.official_review_record(bound['package_ref']['id'])
    assert record is not None and record.verdict is ReviewVerdict.ACCEPT and str(record.purpose) == 'METHOD_PLAN', record
    report['method_plan'] = {'method': pin.to_json(), 'review_key': review_key, 'policy_domain': row['scope_hash'],
                             'scope_present': False, 'invocations': 1, 'intent_state': rt.store.get_intent(intent.intent_id).state,
                             'official_record': str(record.record_id), 'identity_scope': 'mission'}


async def main():
    report = {'scope': 'MISSION_FINAL: original root cut -> assured _ask_root_reviewer -> purpose-domain policy -> '
                       'unified transport -> actual turn -> official record -> READY; METHOD_PLAN: runtime entry '
                       'used by the admission hook, planning-subject policy domain, counters, official record; '
                       'fixture routing/ACL/lease/single-consumer pump, SEMANTIC criteria only; not a real model, '
                       'four-consumer deployment, terminal writers, Host or UI; CONTENT_ONLY root Scope (a MIXED root '
                       'needs its effect proof before the cut: operation path, items 4/7)'}
    with TemporaryDirectory(prefix='assurance-purpose-builders-') as temp:
        root = Path(temp).resolve()
        async with AssuredRuntime(root, [CONTENT_REPLY], content_only=True) as rt:
            verdict, record = await rt.run_critic()
            assert verdict.passed
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            completed = rt.accept_now()
            assert completed.accepted_result_id == rt.stored.envelope.id
            report['leaf'] = {'accepted': True, 'certificates': count(rt.store, 'SELECT COUNT(*) FROM assurance_use_certificates')}
            await mission_final(rt, report)
            await method_plan(rt, report)
            report['code_only'] = {p: 'builder + hook present; original entry needs a compound/operation fixture (not exercised here)'
                                   for p in ('COMPOSITION', 'ACTION_PROPOSAL', 'OPERATION_OUTCOME')}
            report['provider_calls'] = rt.provider.calls
            rt.store.close()
    report['status'] = 'PASS'
    report['source_sha256'] = source_sha256(['orchestrator/assurance_purpose_reviews.py', 'orchestrator/assurance_review_runtime.py',
        'orchestrator/assurance_check_policy.py', 'orchestrator/assurance_review_transport.py', 'orchestrator/event_handler.py',
        'orchestrator/root_review.py', 'orchestrator/composition_review.py', 'orchestrator/operation_runtime.py',
        'orchestrator/assurance_content_review.py', 'assurance/reviews.py'])
    out = EVIDENCE / ('purpose-builders-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out), 'mission_final': report['mission_final']['root_state'],
                      'method_plan': report['method_plan']['official_record'],
                      'counters': {k: report[k] for k in ('mission_final_no_policy', 'method_plan_uncovered', 'method_plan_goal_mismatch',
                                                          'method_plan_no_author', 'method_plan_no_policy')}}, ensure_ascii=False))


asyncio.run(main())
