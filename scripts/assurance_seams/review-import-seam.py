"""One admitted scope + actual format algorithm -> immutable CheckBinding; fixture Mission."""
from seam_paths import SDK, EVIDENCE, seam_tool_ports
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
    import asyncio
    from types import SimpleNamespace, MethodType
    sys.path.insert(0,str(SDK/'tests/agents'))
    from provider_fixture import MODEL, ScriptedProvider
    from simple_harness.agents import AgentConfig, build_agent_runtime
    from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
    from agent_orchestrator.assurance.codec import canonical
    from agent_orchestrator.assurance.certificates import UseIdentity
    from agent_orchestrator.assurance.evidence import build_catalogue,ReadItem
    from agent_orchestrator.assurance.root_gate import CurrentReadPermission
    from agent_orchestrator.assurance.reviews import AssuranceReviewBinding
    from agent_orchestrator.assurance.review_input import render_review_input,REVIEW_INSTRUCTIONS
    from agent_orchestrator.orchestrator.assurance_review_transport import assurance_formula
    from agent_orchestrator.orchestrator.assurance_review_collect import collect_assurance_review
    from agent_orchestrator.orchestrator.assurance_review_import import prepare_official_review,read_imported_review_locked
    from agent_orchestrator.orchestrator.assurance_review_pins import ensure_review_blob_pins
    from agent_orchestrator.orchestrator.commit_service import Reservation
    from agent_orchestrator.orchestrator.completion_inputs import load_completion_result_inputs
    from agent_orchestrator.orchestrator.scoped_content_review import read_task_content_projection
    from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly
    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.agent_worker import AgentBridge
    from agent_orchestrator.storage.assurance_store import AssuranceStore
    cas=ArtifactStore(root/'scoped-cas')
    semantic=HtnStore(store)
    inputs=load_completion_result_inputs(store,stored)
    from agent_orchestrator.orchestrator.scoped_content_review import read_task_check_policy_projection
    checked_projection=read_task_check_policy_projection(store,world.mission.id,task.id)
    projection=SimpleNamespace(scope=checked_projection.scope,criteria=checked_projection.criteria,
        expression=CriterionExpr('criterion-report'))
    taskbinding=semantic.task_semantics_of(world.mission.id,task.id)
    package=LeafAcceptanceAssembly(store,commit)._package(world.mission.id,taskbinding,req,
        stored.envelope.id,inputs.frozen.manifest_hash,('fixture-worker',),projection=projection)
    target=AssuranceRef('result',Pin(stored.envelope.id,0,fingerprint(stored.envelope.to_json())))
    evidence=AssuranceRef('artifact',Pin(artifact.id,artifact.version,artifact.content_hash))
    catalogue=build_catalogue('review-seam', (evidence,))
    scope=projection.scope
    binding=AssuranceReviewBinding.from_json({'schema_version':2,'mission_id':world.mission.id,
        'review_key':'review-seam','package_ref':Pin(str(package.package_id),0,fingerprint(package.to_json())).to_json(),
        'round_no':1,'subject':{'purpose':'TASK_CONTENT','target':target.to_json(),
          'owner_task_ref':Pin(task.id,taskbinding.contract_revision,taskbinding.content_hash()).to_json(),
          'occurrence_id':scope.occurrence_id,'completion_scope_ref':scope_ref.pin.to_json(),
          'method_instance_ref':None,'input_manifest_hash':inputs.frozen.manifest_hash,'output_manifest_hash':None},
        'requirements_ref':policy_command['requirements_ref'].pin.to_json(),
        'criterion_ids':[str(c.criterion_id) for c in package.criteria],
        'mandatory_ids':[str(c.criterion_id) for c in package.criteria if c.requirement_class.value=='HARD_CONSTRAINT'],
        'formula':assurance_formula(package.success_expression),
        'check_requirements':[p.to_json() for p in policy_command['candidate_mapping']],
        'evidence_catalogue':[item.to_json() for item in catalogue],
        'producer_agent_ids':['fixture-worker'],'reviewer_policy_ref':Pin('fixture-review-policy',1,'a'*64).to_json(),
        'context_policy_ref':Pin('fixture-context-policy',1,'b'*64).to_json(),
        'criterion_policy_ref':policy_ref.to_json(),'catalogue_hash':fingerprint([item.to_json() for item in catalogue]),
        'read_set':[ReadItem('OBJECT','fixture-frozen-scope',scope_ref.pin.content_hash).to_json()]})
    message=render_review_input(binding,package=package.to_json(),materials={evidence:cas.path_for(artifact.content_hash).read_bytes()})
    agentconfig=AgentConfig(name='reviewer',instructions=REVIEW_INSTRUCTIONS,model_profile_ref='fixture-review')
    args=dict(tenant_id=world.mission.tenant_id,binding=binding,package=package,request_command_id='review-command',
        config={'agent_config':agentconfig.to_json(),'message':message},reservation=Reservation(tokens=100,cost_micros=0),
        require_current_locked=commit._assurance_root_gate.require_execution)
    invocation=commit.ensure_assurance_review_invocation(**args)
    assert commit.ensure_assurance_review_invocation(**args)==invocation
    async def run():
        response={'schema_version':2,'verdict':'ACCEPT','assessments':[{'criterion_id':str(c.criterion_id),'verdict':'PASS',
            'evidence_ids':[catalogue[0].label],'reason':'fixture response','limitations':[]} for c in package.criteria],'findings':[]}
        provider=ScriptedProvider([canonical(response)])
        async with build_agent_runtime(AgentRuntimePorts(provider=provider,authorization=AllowAllAuthorization(),database_path=str(root/'runtime.db'),model=MODEL,owner_id='review-seam',**seam_tool_ports(root,cas)[1])) as runtime:
            bridge=AgentBridge(runtime,unpriced=True)
            intent=store.get_intent(invocation.to_json()['dispatch_intent_id'])
            commit.claim_intent(intent.intent_id,owner='fixture-orchestrator',lease_seconds=60)
            agent_id,_,_=await bridge.create(creation_key=intent.creation_key,config_json=intent.config['agent_config'])
            turn_id=await bridge.expected_turn_id(agent_id=agent_id,input_id=intent.input_id)
            commit.record_agent_created(intent.intent_id,agent_id=agent_id,expected_turn_id=turn_id)
            submitted=await bridge.submit(agent_id=agent_id,input_id=intent.input_id,message_json=intent.config['message'])
            commit.record_submitted(intent.intent_id,receipt=submitted)
            agent=await runtime.open(agent_id)
            await agent.wait_turn(turn_id,timeout=5)
            intent=store.get_intent(intent.intent_id)
            orch=SimpleNamespace(store=store,commit=commit,bridge_for=lambda _:bridge,
                assembled=SimpleNamespace(workspaces=SimpleNamespace(artifact_store=cas)),
                _expected_model=lambda _:MODEL,_note=lambda _:None)
            for name in ('_settle_intent','_import_usage','_service_agent_ids','_settle_service_if_known'):
                setattr(orch,name,MethodType(getattr(Orchestrator,name),orch))
            await collect_assurance_review(orch,intent)
            await collect_assurance_review(orch,intent)
            receipt_id='assurance-review-classified:'+fingerprint({'intent':intent.intent_id,'turn':turn_id})
            receipt=dict(store.get_receipt(receipt_id))
            assert receipt['classification']=='READY_FOR_CURRENT_REVIEW',receipt
            classification=AssuranceRef('commit_receipt',Pin(receipt_id,0,fingerprint(receipt)))
            with store.read_view():
                reader=AssuranceReader(store,tenant_id=world.mission.tenant_id,mission_id=world.mission.id)
                imported=read_imported_review_locked(commit,reader,classification)
            rawref=AssuranceRef.from_json(decode(imported.turn.body_json)['payload']['raw_output_ref'])
            pins=ensure_review_blob_pins(commit,tenant_id=world.mission.tenant_id,binding=binding,refs=(evidence,rawref))
            root_id=commit._assurance_root_gate.require_execution().root_incarnation_id
            identity=UseIdentity(world.mission.id,'REVIEW','review-seam',scope.scope_id,'fixture-current-consumer','ACCEPT',root_id)
            deadline=int(store.now*1000)+60000
            def authority(identity,ref):
                return CurrentReadPermission(ReadItem('ACCESS',ref.key,fingerprint({'fixture-identity':identity.principal_id,'ref':ref.to_json()})),
                    ReadItem('POLICY','fixture-current-policy','f'*64),deadline)
            prepared=prepare_official_review(commit,tenant_id=world.mission.tenant_id,classification_ref=classification,
                identity=identity,authority=authority,cas=cas,pins=pins)
            refused(lambda:semantic.insert_review_record(prepared.record,official=True),'REVIEW_RUNTIME_IMPORT_REQUIRED')
            from agent_orchestrator.orchestrator.assurance_review_consumer import AssuranceReviewConsumer
            from agent_orchestrator.storage.assurance_work import AssuranceWorkStore
            consumer=AssuranceReviewConsumer(commit,tenant_id=world.mission.tenant_id,
                principal_id=identity.principal_id,authority=authority,cas=cas,check_adapter=None)
            work=AssuranceWorkStore(store)
            cursor=store.connection.execute("SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer='REVIEW'",(world.mission.id,)).fetchone()
            work.ingest(world.mission.id,'REVIEW',expected_version=cursor['row_version'],
                classify=lambda event,kind:consumer.classify(event),now_ms=int(store.now*1000))
            claim,=work.claim_due(world.mission.id,'REVIEW',owner='fixture-review-consumer',now_ms=int(store.now*1000),lease_ms=30000,limit=1)
            durable=await consumer.prepare(claim)
            assert not durable.rejected
            result=work.commit(claim,now_ms=int(store.now*1000),effect=durable.commit)
            assert store.connection.execute("SELECT state FROM assurance_pending_work WHERE mission_id=? AND consumer='REVIEW' AND work_key=?",(world.mission.id,claim.work_key)).fetchone()[0]=='DONE'
            replay=await consumer.prepare(claim)
            with store.transaction():
                assert replay.commit()==result
            assert prepared.import_locked()==result
            official=semantic.official_review_record(package.package_id)
            assert official.verdict.value=='ACCEPT'
            assert official.criteria[0].check_execution.value=='NOT_RUN'
            assert official.criteria[0].verdict.value=='UNKNOWN'
            manifest=decode(prepared.manifest_json)
            assert manifest['criteria'][0]['effective_grade']=='PASS'
            assert commit.ledger.reservation(intent.subject_id)['state']=='SETTLED'
            report={'status':'PASS','scope':'one original review transport/kernel/import; scripted provider, fixture authority and Requirements; no acceptance, UI or real model',
                'provider_calls':provider.calls,'official_records':len(semantic.list_review_records(package.package_id)),
                'complete_query_sets':len(prepared.complete),'semantic_effective_grade':manifest['criteria'][0]['effective_grade'],
                'receipt':result.to_json()}
            report['source_hashes']={str(path.relative_to(SDK)):hashlib.sha256(path.read_bytes()).hexdigest() for pattern in ('src/agent_orchestrator/orchestrator/assurance_review*.py','src/agent_orchestrator/assurance/review*.py','src/agent_orchestrator/runtime/assurance_turn_sources.py','src/agent_orchestrator/storage/htn_store.py') for path in SDK.glob(pattern)}
            output=(EVIDENCE / ('review-import-seam-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'))
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps({'status':report['status'],'scope':report['scope'],'evidence':str(output)},ensure_ascii=False))
    asyncio.run(run())
