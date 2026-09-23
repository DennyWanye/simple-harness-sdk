"""Item 8 seam: startup reconciliation, orphan pins, lease recovery, eventless expiry, clock rollback.

Part A: two real ``Orchestrator`` lifetimes over one evidence root with
``install_assurance`` (a restart). The second start happens with the wall clock
behind the persisted high-water mark: ``reconcile_startup`` records the rollback
(one TimeDiscontinuity per assured Mission) before any claim, the tick ingests
but claims nothing while rolled back, and work resumes once the clock is past
the mark again (AssuranceClockStable).
Part B: the assured fixture Mission on the real ``AssuranceTick``: a review
preparation that "crashes" after acquiring its CAS pin (PREPARING, no binding)
-> startup reconciliation releases it through the receipted transition (bytes
kept, no GC) -> the real review acquires a new pin identity and binds it ->
a REVIEW work claim held by a dead owner is reclaimed only when its lease
elapses and the original invocation is reused (no second Provider call) ->
certificate expiry with no business event is rebuilt at (re)start -> clock
rollback with no business event: TimeDiscontinuity, no claims, resume on
STABLE. Fixture routing/ACL/lease; not a real model, Host or UI; the original
``recover()``/``_bind_startup_tools`` rebinding of a submitted assured review
turn across a process restart is NOT exercised here.
"""
from _assured_fixture import (EVIDENCE, SDK, AssuredRuntime, count, refused, requirements_ref,  # noqa: F401
                              source_sha256, PRINCIPAL, TENANT)
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.contracts.resolution import DeliveryReceipt, DeliveryStage
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_assembly import (
    AssuranceDeploymentPorts, install_assurance, reconcile_startup)
from agent_orchestrator.orchestrator.assurance_consumers import (
    CLOSEOUT_EVENT, NOTIFICATION_EVENT, VALIDITY_CHECKED_EVENT,
    AssuranceCloseoutConsumer, AssuranceNotifyConsumer, AssuranceValidityConsumer)
from agent_orchestrator.orchestrator.assurance_review_collect import collect_assurance_review
from agent_orchestrator.orchestrator.assurance_tick import AssuranceTick
from agent_orchestrator.orchestrator.assurance_validity import acceptance_id_for
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.root_review import RootReviewCoordinator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.assurance_work import AssuranceWorkStore
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

CONTENT_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
                 'evidence_ids': [], 'reason': 'fixture response', 'limitations': []}], 'findings': []}
FINAL_REPLY = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
               'evidence_ids': [], 'reason': 'fixture root review', 'limitations': []}], 'findings': []}


def events_of(store, mission_id, kind):
    return [e for e in store.list_events(mission_id, limit=10_000) if e.type == kind]


def pending(store, mission_id):
    return [dict(r) for r in store.connection.execute(
        'SELECT consumer,work_key,state,tries,wait_reason,owner FROM assurance_pending_work WHERE mission_id=? '
        'ORDER BY consumer,work_key', (mission_id,)).fetchall()]


def pins(store, mission_id):
    return [dict(r) for r in store.connection.execute(
        'SELECT pin_id,review_key,state,row_version,released_at_ms,last_receipt_id FROM assurance_blob_pins '
        'WHERE mission_id=? ORDER BY created_at_ms,pin_id', (mission_id,)).fetchall()]


def environment(store):
    return dict(store.connection.execute('SELECT clock_generation,wall_high_ms,clock_state FROM assurance_environment_state '
                                         'WHERE singleton=1').fetchone())


async def drain(tick, limit=12):
    rounds = 0
    while rounds < limit and await tick.tick():
        rounds += 1
    return rounds


# ------------------------------------------------------------------ part A
async def restart_with_clock_rollback(report):
    with TemporaryDirectory(prefix='assurance-recovery-install-') as temp:
        base = Path(temp).resolve()
        cfg = OrchestratorConfig(evidence_root=base / 'root')
        principal = Principal('fixture-current-user')
        sent, installed, clock = [], {}, {}

        def root_setup(orch):
            orch.commit.install_assurance_root(principal=principal, tenant_id='tenant', command_id='install')
            if clock:
                orch.store._clock = clock['value']

        def assembly(orch):
            installed['value'] = install_assurance(orch, AssuranceDeploymentPorts(
                tenant_id='tenant', principal=principal,
                select_profile=lambda spec: AssurancePolicy() if spec.idempotency_key.startswith('assured') else None,
                notify_transport=sent.append))

        spec = MissionSpec(goal='assured fixture', success_criteria=('c',), tenant_id='tenant', idempotency_key='assured-1',
                           orchestration_semantics_version='hierarchical', planning_protocol_version='planning-decision-v1')
        # Run 1: install, one assured Mission, an idle tick (records the clock high-water mark).
        async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup, startup_assembly=assembly) as orch:
            store, commit = orch.store, orch.commit
            assured, created = commit.create_mission(spec)
            assert created and AssuranceStore(store).lane(assured.id) == 'ASSURANCE_1_1'
            await drain(orch._assurance_tick)
            first = environment(store)
            assert first['clock_state'] == 'STABLE', first
            run1 = {'startup': installed['value'].startup, 'environment': first}
        # Run 2 (restart): the wall clock is behind the persisted high-water mark.
        clock['value'] = lambda: time.time() - 3600
        async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup, startup_assembly=assembly) as orch:
            store, commit = orch.store, orch.commit
            startup = installed['value'].startup
            assert startup['missions'] == 1 and startup['clock_state'] == 'ROLLBACK', startup
            assert startup['clock_generation'] == first['clock_generation'] + 1, (startup, first)
            assert startup['cursors_rebuilt'] == [] and startup['pins_released'] == [] and startup['running_claims'] == []
            discontinuities = events_of(store, assured.id, 'TimeDiscontinuity')
            assert len(discontinuities) == 1 and discontinuities[0].payload['clock_state'] == 'ROLLBACK', discontinuities
            env = environment(store)
            assert env['clock_state'] == 'ROLLBACK' and env['wall_high_ms'] == first['wall_high_ms'], (env, first)
            # A terminal write during the rollback is durable work; the tick ingests it but claims nothing.
            cancelled = commit.cancel_mission(assured.id)
            assert events_of(store, assured.id, NOTIFICATION_EVENT)
            rounds_rolled_back = await drain(orch._assurance_tick)
            work = pending(store, assured.id)
            assert [(w['consumer'], w['state']) for w in work] == [('CLOSEOUT', 'PENDING'), ('NOTIFY', 'PENDING')], work
            assert sent == [] and environment(store)['clock_generation'] == first['clock_generation'] + 1
            # Clock past the mark again: one AssuranceClockStable, the work is claimed and done.
            orch.store._clock = time.time
            rounds_stable = await drain(orch._assurance_tick)
            stable = events_of(store, assured.id, 'AssuranceClockStable')
            assert len(stable) == 1 and environment(store)['clock_state'] == 'STABLE'
            assert environment(store)['clock_generation'] == first['clock_generation'] + 1  # no second generation
            assert [(w['consumer'], w['state']) for w in pending(store, assured.id)] == [('CLOSEOUT', 'DONE'), ('NOTIFY', 'DONE')]
            assert len(sent) == 1 and sent[0]['state_version'] == cancelled.version, sent
            report['restart_rollback'] = {'run1': run1, 'run2_startup': startup, 'discontinuity': discontinuities[0].payload,
                                          'work_during_rollback': work, 'rounds_rolled_back': rounds_rolled_back,
                                          'rounds_stable': rounds_stable, 'environment_after': environment(store), 'sent': sent}
        clock.clear()


# ------------------------------------------------------------------ part B
async def fixture_recovery(root, report):
    async with AssuredRuntime(root, [CONTENT_REPLY], content_only=True) as rt:
        store, commit, mission = rt.store, rt.commit, rt.mission
        gate = commit._assurance_root_gate
        root_id = gate.require_execution().root_incarnation_id
        sent = []
        commit._assurance_validity = rt.validity
        consumers = {'REVIEW': rt.consumer,
                     'VALIDITY': AssuranceValidityConsumer(commit, tenant_id=TENANT),
                     'CLOSEOUT': AssuranceCloseoutConsumer(commit, tenant_id=TENANT, finalizer=commit.finalize_assured_mission),
                     'NOTIFY': AssuranceNotifyConsumer(commit, tenant_id=TENANT, transport=sent.append)}
        tick = AssuranceTick(rt.orch, consumers=consumers, root_incarnation_id=root_id,
                             require_execution_root=gate.require_execution, tenant_id=TENANT)
        rt.pump = tick
        rt.orch._assurance_tick = tick
        shift = {'s': 0.0}
        store._clock = lambda: time.time() + shift['s']
        stored = rt.stored

        def restart():
            return reconcile_startup(rt.orch, tenant_id=TENANT, root_incarnation_id=root_id)

        # 1. A review preparation "crashes" after the CAS pin is acquired and before the package/binding UoW.
        with patch('agent_orchestrator.orchestrator.assurance_content_review.read_pinned_blob', side_effect=KeyboardInterrupt):
            try:
                await rt.orch._run_critic(mission, rt.task, view_id=stored.envelope.attempt_id,
                                          subject_prefix=stored.envelope.attempt_id + ':critic', account_id='budget:' + rt.task.id,
                                          artifacts=(rt.artifact,), test_output=None, attempt_id=stored.envelope.attempt_id)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError('simulated crash did not interrupt the preparation')
        orphan = pins(store, mission.id)
        assert len(orphan) == 1 and orphan[0]['state'] == 'PREPARING', orphan
        assert store.connection.execute('SELECT 1 FROM assurance_review_bindings WHERE mission_id=? AND review_key=?',
                                        (mission.id, orphan[0]['review_key'])).fetchone() is None
        assert rt.provider.calls == 0 and not store.list_intents('PENDING', 'CLAIMED', 'AGENT_CREATED', 'SUBMITTED') or \
            all(i.mission_id != mission.id or i.kind == 'attempt' for i in store.list_intents('PENDING', 'CLAIMED', 'AGENT_CREATED', 'SUBMITTED'))
        blob_path = Path(rt.artifact.storage_uri)
        assert blob_path.is_file()
        # 2. Startup reconciliation releases the abandoned preparation through the receipted transition; bytes stay.
        #    With the wall clock behind the pin's creation (a rollback) the release is deferred, never backdated.
        store._clock = lambda: time.time() - 3600
        rolled = restart()
        assert rolled['clock_state'] == 'ROLLBACK' and rolled['pins_released'] == [] and \
            rolled['pins_release_deferred'] == [orphan[0]['pin_id']], rolled
        assert pins(store, mission.id)[0]['state'] == 'PREPARING'
        store._clock = lambda: time.time() + shift['s']
        startup = restart()
        assert startup['pins_released'] == [orphan[0]['pin_id']] and startup['pins_preparing_with_binding'] == [], startup
        assert startup['clock_state'] == 'STABLE' and startup['running_claims'] == [] and startup['pins_release_deferred'] == []
        released = pins(store, mission.id)
        assert released[0]['state'] == 'RELEASED' and released[0]['released_at_ms'] is not None and released[0]['row_version'] == 2
        receipt = store.get_receipt(released[0]['last_receipt_id'])
        assert receipt and receipt['state'] == 'RELEASED' and receipt['pin_id'] == orphan[0]['pin_id'], receipt
        assert blob_path.is_file() and blob_path.read_bytes()  # no GC: CAS bytes are never deleted
        assert restart()['pins_released'] == []  # idempotent
        report['orphan_pin'] = {'before': orphan, 'rolled_back_startup': rolled, 'startup': startup, 'after': released, 'release_receipt': receipt,
                                'blob_still_present': blob_path.is_file()}
        # 3. The real review takes a new pin identity (the released one never revives), binds it, goes official.
        verdict, record = await rt.run_critic()
        assert verdict.passed
        live = pins(store, mission.id)
        # The released preparation stays history; the real review binds a new identity for the same
        # object (and, after collection, the raw-output pin of the same review).
        assert [p['state'] for p in live][:2] == ['RELEASED', 'BOUND'] and all(p['state'] == 'BOUND' for p in live[2:]), live
        assert live[1]['pin_id'] == orphan[0]['pin_id'] + ':2', live
        rt.record_critic_layer(record)
        rt.settle_fixture_worker()
        accepted = rt.accept_now()
        assert accepted.accepted_result_id == stored.envelope.id
        await drain(tick)
        report['new_pin'] = {'pins': live, 'official_record': str(record.record_id), 'provider_calls': rt.provider.calls}
        # 4. Lease recovery / owner retention on the MISSION_FINAL review import.
        req = HtnStore(store).get_requirements_revision(mission.id, 1)
        commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id, command_id='fixture-mission-final-policy',
            principal=Principal('fixture-authenticated-user'), requirements_ref=requirements_ref(req),
            completion_scope=rt.scope_ref, purpose='MISSION_FINAL', candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
        dispatch = HierarchicalDispatch(store, commit)
        coordinator = RootReviewCoordinator(store, commit, dispatch, scope_id='mission', issued_by='runner-fixture', max_cuts_per_revision=2)
        package = coordinator.cut(mission.id, now_ms=int(store.now * 1000))
        rt.provider.script.append(json.dumps(FINAL_REPLY))
        assert await rt.orch._ask_root_reviewer(mission, coordinator, package) is True, rt.notes[-5:]
        key = [r[0] for r in store.connection.execute(
            "SELECT review_key FROM assurance_review_invocations WHERE mission_id=? AND review_key LIKE ? AND ordinal=1",
            (mission.id, 'assurance-mission-final:%')).fetchall()][0]
        intent = rt.invocation_intent(key)
        intent, answer = await rt.orch._await_service_turn(intent, store.now + 30, attempt_id=None)
        assert answer is not None
        await collect_assurance_review(rt.orch, intent)
        calls_after_collect = rt.provider.calls
        work = AssuranceWorkStore(store)
        cursor = store.connection.execute("SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer='REVIEW'",
                                          (mission.id,)).fetchone()
        work.ingest(mission.id, 'REVIEW', expected_version=cursor['row_version'], classify=lambda e, c: rt.consumer.classify(e),
                    now_ms=int(store.now * 1000))
        dead = work.claim_due(mission.id, 'REVIEW', owner='crashed-runner', now_ms=int(store.now * 1000), lease_ms=1000, limit=8)
        assert len(dead) == 1 and dead[0].work_key == 'review-import:' + key + ':1', dead
        assert restart()['running_claims'] == [f'{mission.id}:REVIEW:{dead[0].work_key}']  # reported, not repaired
        await drain(tick)
        held = [w for w in pending(store, mission.id) if w['work_key'] == dead[0].work_key][0]
        assert held['state'] == 'RUNNING' and held['owner'] == 'crashed-runner', held
        with store.read_view():
            assert HtnStore(store).official_review_record(str(package.package_id)) is None
        shift['s'] += 2.0  # the dead owner's lease elapses
        rounds = await drain(tick)
        done = [w for w in pending(store, mission.id) if w['work_key'] == dead[0].work_key][0]
        assert done['state'] == 'DONE' and done['tries'] == 2, done
        with store.read_view():
            final_record = HtnStore(store).official_review_record(str(package.package_id))
        assert final_record is not None and str(final_record.verdict) == 'ACCEPT'
        ordinals = [r[0] for r in store.connection.execute(
            'SELECT ordinal FROM assurance_review_invocations WHERE mission_id=? AND review_key=?', (mission.id, key)).fetchall()]
        assert ordinals == [1] and rt.provider.calls == calls_after_collect  # original invocation reused, no second call
        report['lease_recovery'] = {'dead_claim': held, 'after_lease_elapsed': done, 'rounds': rounds, 'ordinals': ordinals,
                                    'provider_calls': rt.provider.calls}
        # 5. Certificate expiry with no business event: rebuilt at (re)start, observed by VALIDITY.
        usable = count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=? "
                              "AND json_extract(certificate_json,'$.decision')='USABLE' AND not_after_ms IS NOT NULL", mission.id)
        assert usable >= 1
        shift['s'] += 61.0
        startup = restart()
        assert startup['expiry_events_emitted'] >= 1, startup
        await drain(tick)
        expired = [e.payload for e in events_of(store, mission.id, VALIDITY_CHECKED_EVENT) if 'EXPIRED' in e.payload['reasons']]
        assert expired, [e.payload for e in events_of(store, mission.id, VALIDITY_CHECKED_EVENT)][-3:]
        assert restart()['expiry_events_emitted'] == 0  # nothing new falls due twice
        report['eventless_expiry'] = {'usable_before': usable, 'startup': startup, 'expired_observations': len(expired)}
        # 6. Clock rollback with no business event: TimeDiscontinuity, no claims, resume on STABLE.
        acceptance_id = acceptance_id_for(rt.task.id, stored.envelope.id)
        commit.record_delivery_receipt(mission.id, DeliveryReceipt('delivery-1', mission.id, acceptance_id, DeliveryStage.PERSISTED,
                                                                   int(store.now * 1000)), command_id='fixture-delivery-1')
        before = environment(store)
        disc_before = len(events_of(store, mission.id, 'TimeDiscontinuity'))
        store._clock = lambda: time.time() - 10.0
        rounds_back = await drain(tick)
        disc = events_of(store, mission.id, 'TimeDiscontinuity')
        env = environment(store)
        assert len(disc) == disc_before + 1 and env['clock_state'] == 'ROLLBACK' and env['clock_generation'] == before['clock_generation'] + 1, (len(disc), disc_before, env, before)
        assert env['wall_high_ms'] == before['wall_high_ms']
        closeout_work = [w for w in pending(store, mission.id) if w['consumer'] == 'CLOSEOUT'][0]
        assert closeout_work['state'] == 'PENDING' and closeout_work['owner'] is None, closeout_work
        closeouts_before = len(events_of(store, mission.id, CLOSEOUT_EVENT))
        store._clock = lambda: time.time() + shift['s']
        rounds_forward = await drain(tick)
        assert len(events_of(store, mission.id, 'AssuranceClockStable')) == disc_before + 1 and environment(store)['clock_state'] == 'STABLE'
        assert [w for w in pending(store, mission.id) if w['consumer'] == 'CLOSEOUT'][0]['state'] == 'DONE'
        assert len(events_of(store, mission.id, CLOSEOUT_EVENT)) > closeouts_before
        assert not tick.has_pending(), pending(store, mission.id)
        report['clock_rollback'] = {'before': before, 'during': env, 'work_during': closeout_work, 'rounds_back': rounds_back,
                                    'rounds_forward': rounds_forward, 'after': environment(store)}
        report['work_table'] = pending(store, mission.id)
        report['provider_calls'] = rt.provider.calls
        store.close()


async def main():
    report = {'scope': 'Item 8: startup reconciliation (clock observed before any claim, orphan PREPARING pins released '
                       'through the receipted transition, RUNNING claims reported), lease recovery with owner retention '
                       '(dead claim reclaimed only after its lease, original invocation reused), eventless certificate expiry '
                       'rebuilt at (re)start, clock rollback with no business event (TimeDiscontinuity, no claims, resume on '
                       'STABLE); restore quarantine root is the separate root-gate-seam. Fixture routing/ACL/lease; not a real '
                       'model, Host or UI; recover()/_bind_startup_tools of a submitted assured review turn across a process '
                       'restart is not exercised.'}
    await restart_with_clock_rollback(report)
    with TemporaryDirectory(prefix='assurance-recovery-') as temp:
        await fixture_recovery(Path(temp).resolve(), report)
    report['status'] = 'PASS'
    report['source_sha256'] = source_sha256(['orchestrator/assurance_assembly.py', 'orchestrator/assurance_review_pins.py',
        'orchestrator/assurance_tick.py', 'orchestrator/assurance_clock.py', 'storage/assurance_work.py', 'assurance/expiry.py',
        'assurance/root_gate.py'])
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    out = EVIDENCE / ('recovery-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out),
                      'restart_generation': report['restart_rollback']['run2_startup']['clock_generation'],
                      'orphan_released': len(report['orphan_pin']['startup']['pins_released']),
                      'lease_tries': report['lease_recovery']['after_lease_elapsed']['tries'],
                      'expired': report['eventless_expiry']['expired_observations'],
                      'rollback_generation': report['clock_rollback']['during']['clock_generation']}, ensure_ascii=False))


asyncio.run(main())
