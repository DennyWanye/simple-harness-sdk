"""Targeted wiring check, using explicit fixture authorization; not acceptance."""
from seam_paths import SDK, EVIDENCE
import asyncio
import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.assurance.codec import AssuranceError, canonical, decode, fingerprint
from agent_orchestrator.assurance.evidence import ReadItem
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts import Artifact, Attempt, Budget, ClaimProposal, ResultEnvelope, Task
from agent_orchestrator.orchestrator.assurance_factory import AssuranceMissionFactory
from agent_orchestrator.orchestrator.assurance_local_checks import AssuranceLocalChecks
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.storage.assurance_blobs import read_pinned_blob
from agent_orchestrator.storage.assurance_reads import AssuranceReader
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.assurance_work import CONSUMERS
from agent_orchestrator.storage.store import Store, StoredResult
from agent_orchestrator.verification.deterministic_checks import LayerResult
from agent_orchestrator.verification.verifier_router import VerifierRouter

fixture = importlib.util.spec_from_file_location('coding_fixture', Path(__file__).with_name('tick-factory-seam.py'))
module = importlib.util.module_from_spec(fixture)
fixture.loader.exec_module(module)


async def run():
    with TemporaryDirectory(prefix='assurance-local-check-seam-') as temp:
        root = Path(temp)
        store = Store.open(root/'orchestrator.db', clock=lambda: 100.0)
        commit = CommitService(store)
        side = AssuranceStore(store)
        install = {'root_incarnation_id': 'fixture-root'}
        store.insert_receipt(commit_id='install', kind='AssuranceEnvironmentInstalled',
            subject_id='fixture-root', base_version=None, proposal_hash=fingerprint(install), receipt=install)
        install_ref = AssuranceRef('commit_receipt', Pin('install', 0, fingerprint(install)))
        side.initialize_environment(receipt=install_ref, root_incarnation_id='fixture-root', now_ms=100000)
        commit._assurance_factory = AssuranceMissionFactory(commit, tenant_id='tenant', policy=AssurancePolicy(),
            require_creation_root=lambda: None, requirements=module.fixture_requirements,
            reconcile=lambda mission_id: {kind:() for kind in CONSUMERS})
        mission, _ = commit.create_mission(MissionSpec(goal='fixture', success_criteria=('c',),
            tenant_id='tenant', idempotency_key='local-check-fixture', orchestration_semantics_version='hierarchical',
            planning_protocol_version='planning-decision-v1'))
        task = Task('fixture-task', mission.id, (), (), 'fixture', 'fixture', ('file:answer.txt',),
                    ('format_check','rule_check'), (), Budget(), 1, 'READY', 1)
        store.insert_task(task, ordinal=0)
        attempt = Attempt('fixture-attempt',task.id,mission.id,'worker','fixture-model', 'fixture-prompt',
            'fixture-context',Budget(), None, None, 'VERIFYING', None, 100, 1, 1, 'fixture-create','fixture-input')
        store.insert_attempt(attempt)
        cas = ArtifactStore(root/'artifacts')
        data = b'fixture answer\n'
        blob_hash = cas.put_bytes(data)
        artifact = Artifact('fixture-artifact', mission.id, task.id, attempt.id, 'text', 'answer.txt', 1,
            blob_hash, len(data), 'fixture-worker', storage_uri=str(cas.path_for(blob_hash)))
        store.upsert_artifact(artifact)
        envelope = ResultEnvelope('fixture-result',task.id,attempt.id,'candidate','fixture',
            (ClaimProposal('fixture claim', 0.5),), ('answer.txt',), ('answer.txt',), (), (), (), {}, mission.id)
        store.insert_result(StoredResult(envelope,'fixture-turn','PENDING',None,100,(artifact.id,),()))
        workspace = root/'verify'
        workspace.mkdir()
        (workspace/'answer.txt').write_bytes(data)
        view = Workspace(workspace, attempt.id, False, cas)
        adapter = AssuranceLocalChecks(commit,tenant_id='tenant',cas=cas,require_current_root=lambda:None)
        created_recorders = []
        captured_runs = []
        def factory(inputs):
            recorder = adapter.prepare(inputs)
            created_recorders.append(recorder)
            sink = recorder.persist
            def capture(actual):
                captured_runs.append((sink,actual))
                return sink(actual)
            recorder.persist = capture
            return recorder
        async def critic(output):
            raise AssertionError('critic must not run in this fixture')
        verdict = await VerifierRouter().verify(mission=mission,task=task,envelope=envelope,artifacts=(artifact,),
            verification_copy=view,client_result_id=None,run_critic=critic,local_check_recorder_factory=factory)
        assert verdict.passed
        events = [e for e in store.list_events(mission.id) if e.type == 'AssuranceLocalCheckFinished']
        assert len(events) == 2
        assert all(e.payload['local_check']['execution_state'] == 'SUCCEEDED' for e in events)
        assert all(e.payload['local_check']['assertions'][0]['verdict'] == 'PASS' for e in events)
        for entry in adapter._registry(mission.id).values():
            spec_event = decode(AssuranceReader(store,tenant_id='tenant',mission_id=mission.id)
                .read_exact_metadata(entry.binding.spec_ref).body_json)
            assert spec_event['payload']['implementation_hash'] == adapter.checker_hash
        assert len({e.payload['local_check']['input_manifest_hash'] for e in events}) == 1
        reader = AssuranceReader(store,tenant_id='tenant',mission_id=mission.id)
        for event in events:
            exact = AssuranceRef('local_check_receipt',Pin(event.id,0,fingerprint(event.to_json())))
            assert reader.read_exact_metadata(exact).ref == exact
            output = AssuranceRef.from_json(event.payload['outputs'][0])
            assert json.loads(cas.path_for(output.pin.content_hash).read_bytes())['status'] == 'PASS'
        called = []
        def fail():
            called.append('run')
            raise RuntimeError('fixture actual checker failure')
        failed = created_recorders[0].run('format_check', fail)
        assert called == ['run'] and failed.status == 'ERROR'
        failed_event = [e for e in store.list_events(mission.id) if e.type == 'AssuranceLocalCheckFinished'][-1]
        assert failed_event.payload['local_check']['execution_state'] == 'ERROR'
        assert failed_event.payload['local_check']['assertions'] == []
        sink, actual_run = captured_runs[0]
        first_ref = sink(actual_run)
        assert first_ref.pin.id == events[0].id
        assert store.count_events(mission.id,'AssuranceLocalCheckFinished') == 3
        changed = decode(actual_run.receipt_json)
        changed['finished_at_ms'] += 1
        try:
            sink(replace(actual_run, receipt_json=canonical(changed)))
        except AssuranceError as error:
            assert error.code == 'IMMUTABLE_IDENTITY_CONFLICT'
        else:
            raise AssertionError('same run with changed body was accepted')
        # Corrupt the original receipt in this disposable DB to exercise the
        # exact review finding: correct source_hash but another event reference.
        run_key = fingerprint({'mission_id':mission.id,'run_nonce':decode(actual_run.receipt_json)['run_nonce']})
        receipt_id = 'assurance-local-check:' + run_key
        old_receipt = dict(store.get_receipt(receipt_id))
        wrong = dict(old_receipt)
        wrong['event_ref'] = AssuranceRef('local_check_receipt',Pin(events[1].id,0,fingerprint(events[1].to_json()))).to_json()
        with store.transaction() as connection:
            connection.execute('UPDATE commit_receipts SET receipt_json=? WHERE commit_id=?',(canonical(wrong),receipt_id))
        try:
            sink(actual_run)
        except AssuranceError as error:
            assert error.code == 'IMMUTABLE_IDENTITY_CONFLICT'
        else:
            raise AssertionError('wrong source event reused')
        with store.transaction() as connection:
            connection.execute('UPDATE commit_receipts SET receipt_json=? WHERE commit_id=?',(canonical(old_receipt),receipt_id))

        ref = AssuranceRef('artifact',Pin(artifact.id,1,blob_hash))
        try:
            side.acquire_pin('bad-pin',mission_id=mission.id,review_key='fixture-review',blob_hash=blob_hash,
                             object_ref=ref,receipt=install_ref,now_ms=100000)
        except AssuranceError as error:
            assert error.code == 'PIN_RECEIPT_MISMATCH'
        else:
            raise AssertionError('unrelated receipt accepted as pin provenance')
        body = {'schema_version':1,'receipt_role':'BLOB_LIVENESS_ONLY','mission_id':mission.id,
            'review_key':'fixture-review','package_id':'allocated-fixture-package','pin_id':'good-pin',
            'object_ref':ref.to_json(),'blob_hash':blob_hash,'state':'PREPARING','expected_version':0,'at_ms':100000}
        store.insert_receipt(commit_id='pin-prepared',kind='AssurancePinPrepared',subject_id='good-pin',
            base_version=0,proposal_hash=fingerprint(body),receipt=body)
        side.acquire_pin('good-pin',mission_id=mission.id,review_key='fixture-review',blob_hash=blob_hash,
            object_ref=ref,receipt=AssuranceRef('commit_receipt',Pin('pin-prepared',0,fingerprint(body))),now_ms=100000)
        assert not store.connection.execute('SELECT 1 FROM assurance_review_bindings').fetchone()
        # Explicit authorization fixture: the deployed root/access adapter is
        # deliberately NOT claimed by this coding seam.
        access = lambda ref: ReadItem('ACCESS','fixture-current-principal-root-purpose','a'*64)
        prepared = read_pinned_blob(reader,ref,pin_id='good-pin',review_key='fixture-review',cas=cas,maximum_bytes=100,
            authorize=access,now_ms=lambda:100000)
        assert prepared.data == data
        with store.transaction():
            prepared.require_current_locked(reader,now_ms=100000,authorize=access)
        guard_calls = []
        def revoked_before_return(ref):
            guard_calls.append(1)
            if len(guard_calls) == 2:
                raise AssuranceError('ACCESS_REVOKED')
            return access(ref)
        try:
            read_pinned_blob(reader,ref,pin_id='good-pin',review_key='fixture-review',cas=cas,maximum_bytes=100,
                authorize=revoked_before_return,now_ms=lambda:100000)
        except AssuranceError as error:
            assert error.code == 'ACCESS_REVOKED' and len(guard_calls) == 2
        else:
            raise AssertionError('bytes returned after current authorization was revoked')
        plain = await VerifierRouter().verify(mission=mission,task=task,envelope=envelope,artifacts=(artifact,),
            verification_copy=view,client_result_id=None,run_critic=critic)
        assert plain.passed and [(row.layer,row.status) for row in plain.layers] == [(row.layer,row.status) for row in verdict.layers]
        def changed_workspace_factory(inputs):
            recorder = adapter.prepare(inputs)
            (workspace/'answer.txt').write_bytes(b'changed after manifest freeze')
            return recorder
        changed_verdict = await VerifierRouter().verify(mission=mission,task=task,envelope=envelope,artifacts=(artifact,),
            verification_copy=view,client_result_id=None,run_critic=critic,local_check_recorder_factory=changed_workspace_factory)
        assert not changed_verdict.passed and changed_verdict.layers[0].status == 'ERROR'
        changed_event = [e for e in store.list_events(mission.id) if e.type == 'AssuranceLocalCheckFinished'][-1]
        assert changed_event.payload['local_check']['execution_state'] == 'ERROR'
        result = {'scope':'actual local verifier/import/pin coding seam',
            'authority':'fixture root only; not deployment or product acceptance',
            'checks':['actual format/rule execution -> source events + output CAS','checker digest persisted',
                'actual exception -> ERROR with empty assertions','unrelated pin receipt rejected',
                'exact local-check replay; changed body and substituted event rejected',
                'allocated PREPARING before review package -> exact full CAS read + final metadata barrier',
                'revocation between pre-read and return rejects bytes',
                'unchanged legacy router statuses; workspace changed after freeze -> ERROR']}
        store.close()
        return result


if __name__ == '__main__':
    result = asyncio.run(run())
    target = (EVIDENCE / (Path(__file__).stem + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json'))
    with target.open('x') as output:
        json.dump(result,output,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
