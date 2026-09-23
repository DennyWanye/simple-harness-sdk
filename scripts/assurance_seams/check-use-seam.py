"""One admitted scope + actual format algorithm -> immutable CheckBinding; fixture Mission."""
from seam_paths import SDK, EVIDENCE
import dataclasses
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path[:0]=[str(SDK/'tests/orchestrator/full_target'),str(SDK/'tests/orchestrator/full_target/operation_completion'),
    str(Path(__file__).parent)]
import test_plan_commits as plans
import test_completion_spec_approval as approval
from test_scoped_content_commit import _mixed_world
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.assurance.check_bindings import CheckBinding
from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import AssuranceError,decode,fingerprint
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.assurance.refs import AssuranceRef,Pin
from agent_orchestrator.assurance.root_gate import AssuranceRootGate
from agent_orchestrator.contracts.resolution import RequirementsRevision,AllExpr,CriterionExpr
from agent_orchestrator.contracts.resolution import EvaluationKind
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_factory import AssuranceMissionFactory
from agent_orchestrator.orchestrator.assurance_local_checks import AssuranceLocalChecks
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.storage.assurance_reads import AssuranceReader
from agent_orchestrator.storage.assurance_work import CONSUMERS
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.verification.assurance_local import freeze_verifier_inputs
from agent_orchestrator.verification.deterministic_checks import format_check
from jsonschema import Draft202012Validator
from referencing import Registry,Resource

schemas=SDK/'src/agent_orchestrator/assurance/schemas'
registry=Registry()
for name in ('common.schema.json','check-spec-v1.schema.json','local-check-receipt-v1.schema.json','check-binding-v2.schema.json'):
    doc=json.loads((schemas/name).read_text())
    registry=registry.with_resource(doc['$id'],Resource.from_contents(doc))
def validate(name,body):
    Draft202012Validator(json.loads((schemas/name).read_text()),registry=registry).validate(body)

def requirements(mission,spec):
    return RequirementsRevision(revision_id='completion-requirements-1',mission_id=mission.id,revision=1,
        criteria=(approval._criterion('criterion-report'),approval._criterion('criterion-delivered')),
        success_expression=AllExpr((CriterionExpr('criterion-report'),CriterionExpr('criterion-delivered'))),
        authority_subject='authenticated-user-confirmation')

criterion_mode = [EvaluationKind.SEMANTIC]
def fixture_requirements(mission,spec):
    original=requirements(mission,spec)
    return dataclasses.replace(original,criteria=tuple(dataclasses.replace(c,evaluation_kind=criterion_mode[0])
        if c.criterion_id=='criterion-report' else c for c in original.criteria))

class FixtureCommit(CommitService):
    def __init__(self,store,**kwargs):
        super().__init__(store,**kwargs)
        self._assurance_root_gate=AssuranceRootGate(store,store.path.parent)
        store._assurance_root_gate=self._assurance_root_gate
        self.install_assurance_root(principal=Principal('fixture-authenticated-user'),
            tenant_id='tenant-p23a',command_id='install')
        self._assurance_factory=AssuranceMissionFactory(self,tenant_id='tenant-p23a',policy=AssurancePolicy(),
            require_creation_root=self._assurance_root_gate.require_execution,requirements=fixture_requirements,
            reconcile=lambda _: {consumer:() for consumer in CONSUMERS})
    def create_mission(self,spec,**kwargs):
        return super().create_mission(dataclasses.replace(spec,planning_protocol_version='planning-decision-v1'),**kwargs)

def refused(call,expected):
    try:
        call()
    except AssuranceError as error:
        assert error.code==expected,(error.code,expected)
    else:
        raise AssertionError('not refused')

with TemporaryDirectory(prefix='assurance-check-binding-') as temp:
    root=Path(temp).resolve()
    # Only fixture creation collaborators are changed; actual plan/scope/Attempt writers run.
    with patch.object(plans,'CommitService',FixtureCommit),patch.object(approval,'_bind_new_protocol',lambda _:None),patch.object(approval,'_requirements',lambda w:HtnStore(w.store).get_requirements_revision(w.mission.id,1)):
        world,req,_,task,stored,artifact=_mixed_world(root,accept_result=False,with_output=True)
    store,commit=world.store,world.service
    scope_row=store.connection.execute('SELECT * FROM operation_completion_scopes WHERE mission_id=?',(world.mission.id,)).fetchone()
    scope_ref=AssuranceRef('completion_scope',Pin(scope_row['scope_id'],0,scope_row['scope_hash']))
    policy_command=dict(tenant_id=world.mission.tenant_id,mission_id=world.mission.id,
        command_id='fixture-policy-approval',principal=Principal('fixture-authenticated-user'),
        requirements_ref=AssuranceRef('requirements',Pin(str(req.revision_id),req.revision,req.content_hash())),
        completion_scope=scope_ref,candidate_mapping=(CriterionPolicy('criterion-report','SEMANTIC',()),))
    policy_ref=commit.approve_assurance_check_policy(**policy_command)
    assert commit.approve_assurance_check_policy(**policy_command)==policy_ref
    refused(lambda:commit.approve_assurance_check_policy(**{**policy_command,'candidate_mapping':
        (CriterionPolicy('invented-criterion','SEMANTIC',()),)}),'POLICY_CATALOGUE_MISMATCH')
    assert store.connection.execute('SELECT count(*) FROM assurance_criterion_policies').fetchone()[0]==1
    cas=ArtifactStore(root/'scoped-cas')
    workspace=root/'verify'
    workspace.mkdir()
    (workspace/'answer.json').write_bytes(cas.path_for(artifact.content_hash).read_bytes())
    inputs=freeze_verifier_inputs(mission=world.mission,task=task,envelope=stored.envelope,
        artifacts=(artifact,),verification_copy=Workspace(workspace,stored.envelope.attempt_id,False,cas),
        client_result_id=None,tampered=(),knowledge=None,require_synthesis_knowledge=False,
        action_problems=None,local_code_execution=False,domain=None,assessment_binding=None)
    validate('local-verification-input-v1.schema.json',inputs)
    adapter=AssuranceLocalChecks(commit,tenant_id=world.mission.tenant_id,cas=cas,
        require_current_root=commit._assurance_root_gate.require_execution)
    recorder=adapter.prepare(inputs)
    actual=recorder.run('format_check',lambda:format_check(stored.envelope,client_result_id=None))
    event_ref=AssuranceRef.from_json(actual.detail['assurance_local_check_ref'])
    event=store.list_events(world.mission.id)[-1]
    reader=AssuranceReader(store,tenant_id=world.mission.tenant_id,mission_id=world.mission.id)
    event=decode(reader.read_exact_metadata(event_ref).body_json)
    validate('local-check-receipt-v1.schema.json',event['payload']['local_check'])
    for entry in adapter._registry(world.mission.id).values():
        validate('check-spec-v1.schema.json',decode(reader.read_exact_metadata(entry.binding.spec_ref).body_json)['payload'])
    refused(lambda:commit.import_assurance_check_locked(mission_id=world.mission.id,
        execution_ref=event_ref,completion_scope=scope_ref),'CHECK_IMPORT_TRANSACTION_REQUIRED')
    with store.transaction():
        bound=commit.import_assurance_check_locked(mission_id=world.mission.id,execution_ref=event_ref,completion_scope=scope_ref)
        again=commit.import_assurance_check_locked(mission_id=world.mission.id,execution_ref=event_ref,completion_scope=scope_ref)
        assert bound==again
    body=decode(reader.read_exact_metadata(bound).body_json)
    validate('check-binding-v2.schema.json',body)
    assert CheckBinding.from_json(body).verdict.value==actual.status
    assert store.connection.execute('SELECT count(*) FROM assurance_check_bindings').fetchone()[0]==1
    assert store.count_events(world.mission.id,'AssuranceCheckBound')==1
    # Actual exception remains UNKNOWN, and imports through the same original transaction.
    def failed_check():raise RuntimeError('fixture actual checker exception')
    failed=recorder.run('format_check',failed_check)
    failed_ref=AssuranceRef.from_json(failed.detail['assurance_local_check_ref'])
    unknown=store.connection.execute('SELECT binding_json FROM assurance_check_bindings WHERE execution_ref_hash=?',
        (failed_ref.key,)).fetchone()
    unknown_body=decode(unknown[0])
    validate('check-binding-v2.schema.json',unknown_body)
    assert unknown_body['execution_state']=='ERROR' and unknown_body['verdict']=='UNKNOWN'
    # Max runtime uses the monotonic duration; actual outputs remain recorded but not PASS.
    with patch('agent_orchestrator.assurance.local_checks.time.monotonic_ns',side_effect=[0,121_000_000_000]):
        over=recorder.run('format_check',lambda:format_check(stored.envelope,client_result_id=None))
    over_ref=AssuranceRef.from_json(over.detail['assurance_local_check_ref'])
    over_body=decode(store.connection.execute('SELECT binding_json FROM assurance_check_bindings WHERE execution_ref_hash=?',
        (over_ref.key,)).fetchone()[0])
    assert over_body['execution_state']=='ERROR' and over_body['verdict']=='UNKNOWN' and over_body['evidence_refs']
    validate('check-binding-v2.schema.json',over_body)
    from agent_orchestrator.assurance.certificates import UseIdentity
    from agent_orchestrator.assurance.evidence import ReadItem
    from agent_orchestrator.assurance.root_gate import CurrentReadPermission
    from agent_orchestrator.storage.assurance_store import AssuranceStore
    from agent_orchestrator.orchestrator.assurance_check_use import prepare_local_check_use
    from agent_orchestrator.assurance.codec import canonical
    side=AssuranceStore(store)
    blob_refs=set(CheckBinding.from_json(body).evidence_refs)
    blob_refs.add(AssuranceRef('artifact',Pin(artifact.id,artifact.version,artifact.content_hash)))
    pins={}
    at=int(store.now*1000)
    for idx,ref in enumerate(sorted(blob_refs,key=lambda r:r.key)):
        pin='fixture-check-use-pin-'+str(idx)
        receipt_id='fixture-check-use-receipt-'+str(idx)
        document={'schema_version':1,'receipt_role':'BLOB_LIVENESS_ONLY','mission_id':world.mission.id,
            'review_key':'fixture-use','package_id':'allocated-fixture-use','pin_id':pin,
            'object_ref':ref.to_json(),'blob_hash':ref.pin.content_hash,'state':'PREPARING','expected_version':0,'at_ms':at}
        store.insert_receipt(commit_id=receipt_id,kind='AssurancePinPrepared',subject_id=pin,
            base_version=0,proposal_hash=fingerprint(document),receipt=document)
        side.acquire_pin(pin,mission_id=world.mission.id,review_key='fixture-use',blob_hash=ref.pin.content_hash,
            object_ref=ref,receipt=AssuranceRef('commit_receipt',Pin(receipt_id,0,fingerprint(document))),now_ms=at)
        pins[ref]=pin
    identity=UseIdentity(world.mission.id,'REVIEW','fixture-current-review',scope_ref.pin.id,
        'fixture-authenticated-user','ACCEPT',commit._assurance_root_gate.require_execution().root_incarnation_id)
    authority_state=['current']
    def authority(use,ref):
        assert use==identity
        return CurrentReadPermission(
            ReadItem('ACCESS',canonical({'use':dataclasses.asdict(use),'ref':ref.to_json()}),fingerprint(authority_state)),
            ReadItem('POLICY','fixture-explicit-check-use-policy',fingerprint({'policy':1})),at+10000)
    command=dict(binding_ref=bound,result_ref=AssuranceRef('result',Pin(stored.envelope.id,0,fingerprint(stored.envelope.to_json()))),
        completion_scope=scope_ref,identity=identity,review_key='fixture-use',pins=pins,authority=authority,maximum_blob_bytes=1000000)
    before=store.connection.total_changes
    prepared=prepare_local_check_use(adapter,**command)
    assert store.connection.total_changes==before, 'preparation must be read only'
    with store.transaction():
        normalized=prepared.consume_locked(adapter,identity=identity,authority=authority,now_ms=int(store.now*1000))
        assert normalized.effective.value==body['verdict']
        refused(lambda:prepared.consume_locked(adapter,identity=dataclasses.replace(identity,principal_id='other'),authority=authority,now_ms=int(store.now*1000)),'CHECK_USE_IDENTITY')
        refused(lambda:prepared.consume_locked(adapter,identity=identity,authority=authority,now_ms=at+10000),'CHECK_USE_EXPIRED')
    saved_environment=adapter.environment_hash
    adapter.environment_hash='f'*64
    with store.transaction():
        refused(lambda:prepared.consume_locked(adapter,identity=identity,authority=authority,now_ms=int(store.now*1000)),'RECHECK_REQUIRED')
    adapter.environment_hash=saved_environment
    authority_state[0]='revoked'
    with store.transaction():
        refused(lambda:prepared.consume_locked(adapter,identity=identity,authority=authority,now_ms=int(store.now*1000)),'RECHECK_REQUIRED')
    authority_state[0]='current'
    refused(lambda:prepare_local_check_use(adapter,**{**command,'identity':dataclasses.replace(identity,purpose='MAINTAIN')}),'CHECK_CONTINUOUS_COVERAGE_UNAVAILABLE')
    # Failed CAS preparation is neither a new receipt nor a normalized PASS.
    output=next(iter(CheckBinding.from_json(body).evidence_refs))
    path=cas.path_for(output.pin.content_hash)
    original=path.read_bytes()
    path.chmod(0o600)  # Owned temporary fixture CAS only.
    path.write_bytes(b'corrupt')
    refused(lambda:prepare_local_check_use(adapter,**command),'REF_BODY_CONFLICT')
    path.write_bytes(original)
    # An imported new assertion invalidates the complete set, even if not cited.
    recorder.run('rule_check',lambda:__import__('agent_orchestrator.verification.deterministic_checks',fromlist=['LayerResult']).LayerResult('rule_check','FAIL','fixture contrary branch',{}))
    with store.transaction():
        refused(lambda:prepared.consume_locked(adapter,identity=identity,authority=authority,now_ms=int(store.now*1000)),'RECHECK_REQUIRED')
    report={'scope':'current exact local check preparation/consume coding seam; explicit fixture authority/pin producer; actual Store, admitted scope, format algorithm and CAS',
        'read_only_preparation':True,'original_receipt_chain':True,'normalized_grade':normalized.effective.value,
        'caller_swap_refused':True,'adapter_replacement_refused':True,'authority_change_refused':True,'half_open_expiry_refused':True,
        'point_check_not_continuous':True,'corrupt_CAS_refused':True,'unreferenced_new_branch_invalidates':True,
        'complete_query_sets':len(prepared.complete_reads),'raw_authority_model_Host_UI':'NOT_COVERED',
        'source_sha256':{name:hashlib.sha256((SDK/'src/agent_orchestrator'/name).read_bytes()).hexdigest()
            for name in ('assurance/check_specs.py','assurance/check_bindings.py','orchestrator/assurance_check_import.py',
                'orchestrator/assurance_check_use.py','orchestrator/assurance_local_checks.py','storage/assurance_store.py',
                'orchestrator/assurance_check_policy.py','orchestrator/scoped_content_review.py')}}
    store.close()
    out=(EVIDENCE / ('check-use-seam-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'))
    with out.open('x') as f:json.dump(report,f,indent=2)
    print(out)
