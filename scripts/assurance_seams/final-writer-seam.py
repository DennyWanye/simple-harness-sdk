"""Item 7 seam: assured root resolution -> judge -> closeout -> unique final writer -> NOTIFY.

Part A drives the real ``Orchestrator`` startup with ``install_assurance`` and
cancels an assured Mission through the original writer: the terminal write
requests the NOTIFY transport (MissionCancelled) and the tick delivers it once.
Part B runs the real ``AssuranceTick`` (REVIEW / VALIDITY / CLOSEOUT / NOTIFY)
on the assured fixture Mission: leaf accept -> MISSION_FINAL official record ->
production trigger ``attempt_root_resolution`` licensed by the current
MISSION_FINAL UseCertificate (ROOT_RESOLUTION consumer; resolution restates the
bound manifest, not the UNKNOWN projection) -> closeout NOT_READY
(MISSION_JUDGMENT_MISSING) -> ``judge_mission`` records the judgment and stays
ACTIVE -> DRAINING (the fixture Worker never ran: its intent / reservation /
usage cannot be closed by the assured settlement reader, so READY is NOT derived
here and the Mission honestly stays ACTIVE) -> final writer contract test on a
hand-set READY row: ``finalize_assured_mission`` (COMPLETED, FINALIZED row,
MissionCompleted, notification request) -> NOTIFY sent once -> post-final
re-evaluations are receipts only. Fixture routing/ACL/lease and a seam-supplied
Mission judgment; not a real model, Host or UI; the derived READY -> FINALIZED
loop needs a real executor run (item 10); no recovery/pin reconciliation (item 8).
"""
from _assured_fixture import (EVIDENCE, SDK, AssuredRuntime, count, refused, requirements_ref,  # noqa: F401
                              source_sha256, PRINCIPAL, TENANT)
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import AssuranceError
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.contracts.resolution import DeliveryReceipt, DeliveryStage
from agent_orchestrator.governance.budgets import BudgetError, UsageFact
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_assembly import AssuranceDeploymentPorts, install_assurance
from agent_orchestrator.orchestrator.assurance_consumers import (
    CLOSEOUT_EVENT, NOTIFICATION_EVENT, NOTIFIED_EVENT, VALIDITY_CHECKED_EVENT,
    AssuranceCloseoutConsumer, AssuranceNotifyConsumer, AssuranceValidityConsumer)
from agent_orchestrator.orchestrator.assurance_final_writer import (
    CLOSEOUT_REQUESTED_EVENT, FINALIZED_KIND, finalize_assured_mission)
from agent_orchestrator.orchestrator.assurance_tick import AssuranceTick
from agent_orchestrator.orchestrator.assurance_validity import ROOT_RESOLUTION_CONSUMER, acceptance_id_for
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


def certificates(store, mission_id):
    return [dict(r) for r in store.connection.execute(
        'SELECT certificate_id,consumer_kind,consumer_id,purpose FROM assurance_use_certificates WHERE mission_id=? '
        'ORDER BY certificate_id', (mission_id,)).fetchall()]


async def drain(tick, limit=12):
    rounds = 0
    while rounds < limit and await tick.tick():
        rounds += 1
    return rounds


# ------------------------------------------------------------------ part A
async def terminal_notice_on_production_install(report):
    with TemporaryDirectory(prefix='assurance-final-writer-install-') as temp:
        base = Path(temp).resolve()
        cfg = OrchestratorConfig(evidence_root=base / 'root')
        principal = Principal('fixture-current-user')
        sent, installed = [], {}

        def root_setup(orch):
            orch.commit.install_assurance_root(principal=principal, tenant_id='tenant', command_id='install')

        def assembly(orch):
            installed['value'] = install_assurance(orch, AssuranceDeploymentPorts(
                tenant_id='tenant', principal=principal,
                select_profile=lambda spec: AssurancePolicy() if spec.idempotency_key.startswith('assured') else None,
                notify_transport=sent.append))

        async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup, startup_assembly=assembly) as orch:
            inst = installed['value']
            store, commit = orch.store, orch.commit
            closeout = inst.consumers['CLOSEOUT']
            # The installer binds the real final writer unless the deployment overrides it.
            assert closeout.finalizer is not None and closeout.finalizer.func is finalize_assured_mission, closeout.finalizer
            assured, created = commit.create_mission(MissionSpec(
                goal='assured fixture', success_criteria=('c',), tenant_id='tenant', idempotency_key='assured-1',
                orchestration_semantics_version='hierarchical', planning_protocol_version='planning-decision-v1'))
            assert created and AssuranceStore(store).lane(assured.id) == 'ASSURANCE_1_1'
            assert not commit.assured_closeout_pending(assured.id)
            legacy, _ = commit.create_mission(MissionSpec(goal='legacy', success_criteria=('c',), tenant_id='tenant',
                                                          idempotency_key='legacy-1'))
            # Original terminal writer on the assured lane: the NOTIFY request is part of the write.
            cancelled = commit.cancel_mission(assured.id)
            final = events_of(store, assured.id, 'MissionCancelled')[0]
            requests = [e.payload for e in events_of(store, assured.id, NOTIFICATION_EVENT)]
            assert requests == [{'final_event_id': final.id, 'state_version': cancelled.version,
                                 'final_event_type': 'MissionCancelled'}], requests
            # A legacy Mission's terminal write requests nothing.
            commit.cancel_mission(legacy.id)
            assert not events_of(store, legacy.id, NOTIFICATION_EVENT)
            rounds = await drain(orch._assurance_tick)
            assert sent == [{'mission_id': assured.id, 'event_id': final.id, 'state_version': cancelled.version}], sent
            await drain(orch._assurance_tick)
            assert len(sent) == 1 and not orch._assurance_tick.has_pending()
            # A cancelled assured Mission never reaches the final writer: no row, no closeout request.
            assert closeout_row(store, assured.id) is None and not events_of(store, assured.id, CLOSEOUT_REQUESTED_EVENT)
            report['production_install'] = {'finalizer': 'assurance_final_writer.finalize_assured_mission',
                                            'cancel_notice': requests, 'sent': sent, 'notify_rounds': rounds,
                                            'legacy_notice': []}


# ------------------------------------------------------------------ part B
async def final_writer(root, report):
    async with AssuredRuntime(root, [CONTENT_REPLY], content_only=True) as rt:
        store, commit, mission = rt.store, rt.commit, rt.mission
        gate = commit._assurance_root_gate
        root_id = gate.require_execution().root_incarnation_id
        sent = []
        commit._assurance_validity = rt.validity  # the deployment's evaluator, as install_assurance binds it
        consumers = {'REVIEW': rt.consumer,
                     'VALIDITY': AssuranceValidityConsumer(commit, tenant_id=TENANT),
                     'CLOSEOUT': AssuranceCloseoutConsumer(commit, tenant_id=TENANT,
                                                           finalizer=commit.finalize_assured_mission),
                     'NOTIFY': AssuranceNotifyConsumer(commit, tenant_id=TENANT, transport=sent.append)}
        tick = AssuranceTick(rt.orch, consumers=consumers, root_incarnation_id=root_id,
                             require_execution_root=gate.require_execution, tenant_id=TENANT)
        rt.pump = tick
        rt.orch._assurance_tick = tick
        # 1. Leaf: official TASK_CONTENT review, UNKNOWN usage (the fixture Worker never ran), acceptance.
        verdict, record = await rt.run_critic()
        assert verdict.passed
        rt.record_critic_layer(record)
        rt.settle_fixture_worker()
        accepted = rt.accept_now()
        assert accepted.accepted_result_id == rt.stored.envelope.id
        await drain(tick)
        assert [e.payload['state'] for e in events_of(store, mission.id, CLOSEOUT_EVENT)][-1] == 'NOT_READY'
        # 2. MISSION_FINAL through the original root cut and the original reviewer entry.
        req = HtnStore(store).get_requirements_revision(mission.id, 1)
        commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id, command_id='fixture-mission-final-policy',
            principal=Principal('fixture-authenticated-user'), requirements_ref=requirements_ref(req),
            completion_scope=rt.scope_ref, purpose='MISSION_FINAL', candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
        dispatch = HierarchicalDispatch(store, commit)
        coordinator = RootReviewCoordinator(store, commit, dispatch, scope_id='mission', issued_by='runner-fixture', max_cuts_per_revision=2)
        package = coordinator.cut(mission.id, now_ms=int(store.now * 1000))
        rt.provider.script.append(json.dumps(FINAL_REPLY))
        assert await rt.orch._ask_root_reviewer(mission, coordinator, package) is True, rt.notes[-5:]
        keys = [r[0] for r in store.connection.execute(
            "SELECT review_key FROM assurance_review_invocations WHERE mission_id=? AND review_key LIKE ? AND ordinal=1",
            (mission.id, 'assurance-mission-final:%')).fetchall()]
        assert keys, keys
        await rt.drive_review(keys[0])
        assert coordinator.state(mission.id).status is RootReviewStatus.READY, coordinator.state(mission.id)
        with store.read_view():
            final_record = HtnStore(store).official_review_record(str(package.package_id))
        projection = {str(c.criterion_id): str(c.verdict) for c in final_record.criteria}
        assert projection == {'criterion-report': 'UNKNOWN'}, projection  # SEMANTIC PASS lives in the bound manifest
        # 3. The production trigger forms the assured root resolution under the current MISSION_FINAL use.
        principal = PlanPrincipal('fixture-authenticated-user')
        inputs = dispatch.root_resolution_inputs(mission.id)
        assert inputs.complete and inputs.root_form == 'primitive' and inputs.method_instance_id is None, inputs
        outcome = dispatch.attempt_root_resolution(mission.id, principal=principal, command_id='fixture-root-resolution')
        assert outcome.committed, outcome
        resolution = HtnStore(store).get_goal_resolution(outcome.resolution_id)
        assert {str(c.criterion_id): str(c.verdict) for c in resolution.criteria} == {'criterion-report': 'PASS'}
        certs = certificates(store, mission.id)
        root_certs = [c for c in certs if c['consumer_kind'] == ROOT_RESOLUTION_CONSUMER]
        assert root_certs and root_certs[0]['consumer_id'] == outcome.resolution_id and root_certs[0]['purpose'] == 'ACCEPT', certs
        committed = events_of(store, mission.id, 'GoalResolutionCommitted')[-1].payload
        assert committed['witness_id'] == root_certs[0]['certificate_id'] and committed['is_mission_root'], committed
        assert not events_of(store, mission.id, 'RootGoalResolutionRefused')
        assert rt.validity.candidate_for(mission.id, str(final_record.record_id)) is None  # forgotten after the commit
        again = dispatch.attempt_root_resolution(mission.id, principal=principal, command_id='fixture-root-resolution-2')
        assert again.committed and again.reason == 'ALREADY_RESOLVED', again
        await drain(tick)
        closeouts = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)]
        assert closeouts[-1]['state'] == 'NOT_READY' and closeouts[-1]['reasons'] == ['MISSION_JUDGMENT_MISSING'], closeouts[-1]
        row = closeout_row(store, mission.id)
        assert row and row['state'] == 'NOT_READY' and row['row_version'] == 1 and row['resolution_id'] == outcome.resolution_id, row
        report['root_resolution'] = {'outcome': outcome.reason or 'committed', 'resolution_id': outcome.resolution_id,
                                     'record_projection': projection,
                                     'resolution_criteria': {str(c.criterion_id): str(c.verdict) for c in resolution.criteria},
                                     'licence': root_certs[0], 'second_attempt': again.reason,
                                     'closeout': closeouts[-1], 'row': row}
        # 4. The final writer refuses outside a READY row (nothing is written).
        with store.transaction():
            assert refused(lambda: finalize_assured_mission(commit, mission.id, {**closeouts[-1], 'state': 'READY',
                                                                                 'mission_version': store.get_mission(mission.id).version}),
                           {'CLOSEOUT_NOT_READY'})
        assert store.get_mission(mission.id).status.value == 'ACTIVE' and not events_of(store, mission.id, 'MissionCompleted')
        # 5. judge_mission on the assured lane: records the judgment, requests closeout, stays ACTIVE.
        #    (The judgment itself is supplied by the seam: the Mission judge's evaluation is not under test here.)
        criteria = list(store.get_mission(mission.id).success_criteria)
        judged = commit.judge_mission(mission.id, judgments=[{'criterion': c, 'met': True, 'judge': 'seam'} for c in criteria],
                                      summary='seam judgment')
        assert judged.status.value == 'ACTIVE' and judged.final_report['assurance_judgment']['met'] is True, judged
        assert commit.assured_closeout_pending(mission.id)
        assert events_of(store, mission.id, 'MissionSuccessJudged') and events_of(store, mission.id, CLOSEOUT_REQUESTED_EVENT)
        assert not events_of(store, mission.id, 'MissionCompleted')
        await drain(tick)
        closeouts = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)]
        open_state = {'intents': [(i.intent_id, i.kind, i.state, i.subject_id) for i in store.list_intents(
                          'PENDING', 'CLAIMED', 'AGENT_CREATED', 'SUBMITTED') if i.mission_id == mission.id],
                      'reservations': [dict(r) for r in store.connection.execute(
                          "SELECT subject_id,state FROM budget_reservations WHERE mission_id=?", (mission.id,)).fetchall()]}
        assert closeouts[-1]['state'] == 'DRAINING' and 'USAGE_UNKNOWN' in closeouts[-1]['reasons'], (closeouts[-1], open_state)
        row = closeout_row(store, mission.id)
        assert row['state'] == 'DRAINING' and row['row_version'] == 2, row
        assert store.get_mission(mission.id).status.value == 'ACTIVE'
        report['judged'] = {'mission_status': 'ACTIVE', 'judgment': judged.final_report['assurance_judgment'],
                            'closeout': closeouts[-1], 'row': row}
        # 6. Honest fixture limit: the fixture Worker never ran in the AgentRuntime, so its attempt intent
        #    (SUBMITTED with a fixture receipt), its RESERVED reservation and its UNKNOWN usage cannot be closed
        #    through the assured settlement reader (it cross-checks the runtime's agent binding / turn /
        #    invocations / grants by design). The closeout therefore stays DRAINING and the Mission stays
        #    ACTIVE: no no-progress pseudo failure, the original ledger keeps its hold.
        assert closeouts[-1]['reasons'] == ['OPEN_INTENTS', 'OPEN_RESERVATIONS', 'USAGE_UNKNOWN'], closeouts[-1]
        try:
            commit.settle_subject(rt.stored.envelope.attempt_id, mission.id, task_id=rt.task.id)
        except BudgetError as error:
            settle_refusal = str(error)
        else:
            raise AssertionError('fixture executor settled without runtime facts')
        assert 'reservation held' in settle_refusal, settle_refusal
        open_state['settle_refusal'] = settle_refusal
        commit.import_usage(rt.stored.envelope.attempt_id, mission.id, (UsageFact('fixture-worker-usage', 10, 10, 20),))
        acceptance_id = acceptance_id_for(rt.task.id, rt.stored.envelope.id)
        commit.record_delivery_receipt(mission.id, DeliveryReceipt('delivery-1', mission.id, acceptance_id, DeliveryStage.PERSISTED,
                                                                   int(store.now * 1000)), command_id='fixture-delivery-1')
        await drain(tick)
        closeouts = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)]
        assert closeouts[-1]['state'] == 'DRAINING' and closeouts[-1]['reasons'] == ['OPEN_INTENTS', 'OPEN_RESERVATIONS'], closeouts[-1]
        assert store.get_mission(mission.id).status.value == 'ACTIVE' and commit.assured_closeout_pending(mission.id)
        row = closeout_row(store, mission.id)
        assert row['state'] == 'DRAINING' and row['row_version'] == 3, row
        assert not events_of(store, mission.id, 'MissionCompleted') and not events_of(store, mission.id, NOTIFICATION_EVENT)
        report['draining'] = {'open': open_state, 'after_known_usage_and_delivery': closeouts[-1], 'row': row,
                              'usage_flags': commit._ledger.usage_flags(mission.id),
                              'note': 'fixture worker never ran in the runtime: executor cannot be closed here (same gap as '
                                      'handoff item 2 worker settlement positive case); READY is not derived on this fixture'}
        # 7. Final writer contract test (NOT the derived closed loop): the seam sets the row READY by hand and
        #    calls the unique final writer with a READY evaluation, then the real consumers take over
        #    (MissionCompleted -> re-evaluation is a receipt only; notification request -> NOTIFY once).
        with store.transaction():
            store.connection.execute("UPDATE assurance_closeouts SET state='READY',row_version=? WHERE mission_id=? AND row_version=?",
                                     (row['row_version'] + 1, mission.id, row['row_version']))
        active = store.get_mission(mission.id)
        evaluation = {'mission_id': mission.id, 'state': 'READY', 'resolution_id': row['resolution_id'],
                      'mission_version': active.version, 'reasons': [], 'seam': 'hand-set READY row (writer contract test)'}
        with store.transaction():
            # Wrong Mission version / wrong resolution refuse before anything is written.
            assert refused(lambda: finalize_assured_mission(commit, mission.id, {**evaluation, 'mission_version': active.version + 1}),
                           {'RECHECK_REQUIRED'})
            assert refused(lambda: finalize_assured_mission(commit, mission.id, {**evaluation, 'resolution_id': 'res-other'}),
                           {'RECHECK_REQUIRED'})
        assert store.get_mission(mission.id).status.value == 'ACTIVE' and not events_of(store, mission.id, 'MissionCompleted')
        with store.transaction():
            ref = finalize_assured_mission(commit, mission.id, evaluation)
        completed = store.get_mission(mission.id)
        assert completed.status.value == 'COMPLETED' and completed.stop_reason == 'verification_passed', completed
        row = closeout_row(store, mission.id)
        assert row['state'] == 'FINALIZED' and row['row_version'] == 5, row
        finals = events_of(store, mission.id, 'MissionCompleted')
        assert len(finals) == 1
        finalized = events_of(store, mission.id, FINALIZED_KIND)
        assert len(finalized) == 1 and finalized[0].payload['final_event_id'] == finals[0].id
        receipt = store.get_receipt('assurance-finalized:' + mission.id)
        assert receipt and receipt['closeout_row_version'] == 5 and receipt['state_version'] == completed.version, receipt
        assert ref.pin.id == 'assurance-finalized:' + mission.id and row['last_receipt_id'] == ref.pin.id
        assert completed.final_report['assurance_closeout']['finalized_receipt_id'] == ref.pin.id
        assert completed.final_report['assurance_judgment']['met'] is True
        requests = [e.payload for e in events_of(store, mission.id, NOTIFICATION_EVENT)]
        assert requests == [{'final_event_id': finals[0].id, 'state_version': completed.version,
                             'final_event_type': 'MissionCompleted'}], requests
        assert not commit.assured_closeout_pending(mission.id)
        rounds = await drain(tick)
        assert sent == [{'mission_id': mission.id, 'event_id': finals[0].id, 'state_version': completed.version}], sent
        assert len(events_of(store, mission.id, NOTIFIED_EVENT)) == 1
        assert closeout_row(store, mission.id) == row
        last = [e.payload for e in events_of(store, mission.id, CLOSEOUT_EVENT)][-1]
        assert last['state'] == 'FINALIZED' and last['reasons'] == ['ALREADY_FINALIZED'], last
        await drain(tick)
        assert len(events_of(store, mission.id, 'MissionCompleted')) == 1 and len(sent) == 1
        assert not tick.has_pending(), pending(store, mission.id)
        with store.transaction():
            assert refused(lambda: finalize_assured_mission(commit, mission.id, {**evaluation, 'mission_version': completed.version}),
                           {'CLOSEOUT_NOT_READY'})
        report['final_writer_contract'] = {'note': 'READY row set by the seam, not derived by the consumer (see draining.note)',
                                           'rounds': rounds, 'mission_status': completed.status.value,
                                           'stop_reason': completed.stop_reason, 'row': row, 'receipt': receipt,
                                           'notification_requests': requests, 'sent': sent, 'post_final': last,
                                           'refusals': ['RECHECK_REQUIRED(version)', 'RECHECK_REQUIRED(resolution)',
                                                        'CLOSEOUT_NOT_READY(after FINALIZED)'],
                                           'validity_checks': len(events_of(store, mission.id, VALIDITY_CHECKED_EVENT))}
        report['work_table'] = pending(store, mission.id)
        report['provider_calls'] = rt.provider.calls
        store.close()


async def main():
    report = {'scope': 'Item 7: assured root resolution licensed by the current MISSION_FINAL UseCertificate (production '
                       'trigger, ROOT_RESOLUTION consumer, resolution restates the bound manifest) -> judge_mission records the '
                       'judgment and stays ACTIVE -> closeout NOT_READY/DRAINING/READY -> unique final writer (COMPLETED, '
                       'FINALIZED row, MissionCompleted, notification request) -> NOTIFY once; original terminal writers on the '
                       'assured lane request NOTIFY (cancel on the real Orchestrator install). Fixture routing/ACL/lease, '
                       'seam-supplied Mission judgment; READY is not derived on this fixture (worker never ran) so the '
                       'final writer is proven by a contract test on a hand-set READY row; not a real model, Host or UI; '
                       'no recovery/pin reconciliation (item 8).'}
    await terminal_notice_on_production_install(report)
    with TemporaryDirectory(prefix='assurance-final-writer-') as temp:
        await final_writer(Path(temp).resolve(), report)
    report['status'] = 'PASS'
    report['source_sha256'] = source_sha256(['orchestrator/assurance_final_writer.py', 'orchestrator/assurance_consumers.py',
        'orchestrator/assurance_assembly.py', 'orchestrator/assurance_validity.py', 'orchestrator/resolution_commits.py',
        'orchestrator/hierarchical_dispatch.py', 'orchestrator/commit_service.py', 'orchestrator/event_handler.py',
        'verification/scoped_acceptance.py'])
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    out = EVIDENCE / ('final-writer-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out), 'root_resolution': report['root_resolution']['outcome'],
                      'draining': report['draining']['after_known_usage_and_delivery']['reasons'],
                      'final_writer_contract': report['final_writer_contract']['mission_status'],
                      'sent': len(report['final_writer_contract']['sent'])}, ensure_ascii=False))


asyncio.run(main())
