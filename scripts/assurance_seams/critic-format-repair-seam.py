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
    from agent_orchestrator.orchestrator.assurance_content_review import ensure_task_content_review
    from agent_orchestrator.orchestrator.assurance_review_transport import read_review_invocation_locked
    from agent_orchestrator.orchestrator.scoped_content_review import read_task_content_candidate
    from agent_orchestrator.assurance.evidence import CatalogueEntry
    projection=read_task_content_candidate(store,world.mission.id,task.id,stored.envelope.id)
    scope=projection.scope
    evidence=AssuranceRef('artifact',Pin(artifact.id,artifact.version,artifact.content_hash))
    deadline=int(store.now*1000)+60000
    def builder_authority(identity,ref):
        return CurrentReadPermission(ReadItem('ACCESS',ref.key,fingerprint({'fixture-principal':identity.principal_id,'purpose':identity.purpose,'ref':ref.to_json()})),
            ReadItem('POLICY','fixture-current-policy','f'*64),deadline)
    from agent_orchestrator.orchestrator.assurance_review_runtime import AssuranceReviewRuntime
    from agent_orchestrator.orchestrator.assurance_review_consumer import AssuranceReviewConsumer
    from agent_orchestrator.storage.assurance_work import AssuranceWorkStore
    from agent_orchestrator.orchestrator.assurance_tick import PreparedAssuranceWork
    from agent_orchestrator.runtime.first_request_budget import FirstRequestBudgetUnknown
    class ReviewPump:
        # Explicit single-consumer fixture: not the four-consumer deployment.
        def __init__(self,consumer):
            self.consumers={'REVIEW':consumer}
            self.work=AssuranceWorkStore(store)
        async def tick(self):
            cursor=store.connection.execute("SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer='REVIEW'",(world.mission.id,)).fetchone()
            consumer=self.consumers['REVIEW']
            self.work.ingest(world.mission.id,'REVIEW',expected_version=cursor['row_version'],classify=lambda e,c:consumer.classify(e),now_ms=int(store.now*1000))
            for claim in self.work.claim_due(world.mission.id,'REVIEW',owner='runner-fixture',now_ms=int(store.now*1000),lease_ms=30000,limit=1):
                prepared=await consumer.prepare(claim)
                assert isinstance(prepared,PreparedAssuranceWork),prepared
                self.work.commit(claim,now_ms=int(store.now*1000),effect=prepared.commit,rejected=prepared.rejected)
    from simple_harness.providers.base import ProviderUsage
    class KnownUsageProvider(ScriptedProvider):
        async def invoke(self,request,*,cancel):
            result=await super().invoke(request,cancel=cancel)
            return dataclasses.replace(result,usage=ProviderUsage(10,10,20))
    async def run():
        response={'schema_version':2,'verdict':'ACCEPT','assessments':[{'criterion_id':'criterion-report','verdict':'PASS',
            'evidence_ids':[],'reason':'fixture response','limitations':[]}],'findings':[]}
        provider=KnownUsageProvider(['{malformed',canonical(response)])
        async with build_agent_runtime(AgentRuntimePorts(provider=provider,authorization=AllowAllAuthorization(),database_path=str(root/'runtime.db'),model=MODEL,owner_id='runner-seam')) as runtime:
            bridge=AgentBridge(runtime,unpriced=True)
            orch=SimpleNamespace(store=store,commit=commit,bridge_for=lambda _:bridge,
                assembled=SimpleNamespace(workspaces=SimpleNamespace(artifact_store=cas),pool=lambda _:SimpleNamespace(bridge=bridge),gateway=SimpleNamespace(unbind=lambda _:None)),
                _expected_model=lambda _:MODEL,_note=lambda _:None,_assurance_reviews=None,
                _owner='runner-fixture',_poll=0.001,_critic_wait=10,
                _config=SimpleNamespace(lease_seconds=60,turn_deadline_seconds=10,critic_reserve_tokens=100),
                _assurance_root_gate=commit._assurance_root_gate,_assurance_management_only=False,
                _route_service=lambda *a:SimpleNamespace(profile_id='fixture-review'),
                _service_config=lambda d:{'runtime_profile_id':d.profile_id,'model':MODEL},
                _reservation=lambda *a:Reservation(tokens=100,cost_micros=0),
                _assembly_missing=lambda *a,**k:False,_pool_missing=lambda _:False,
                _context_profile_for=lambda _:None,_fault=lambda *a:None,
                _hold_lease=lambda _:None,_service_blocked_since={},
                _validate_mission_judge_intent=lambda _:None)
            for name in ('_run_critic','_dispatch','_dispatch_until_submitted','_await_service_turn',
                '_critic_subject_stopped','_bind_critic','_require_assurance_execution_root',
                '_settle_intent','_import_usage','_service_agent_ids','_settle_service_if_known','profile_of'):
                setattr(orch,name,MethodType(getattr(Orchestrator,name),orch))
            async def normal_wait(intent,liveness):
                assert not Orchestrator._provider_blocked(liveness),'fixture unexpectedly blocked'
                return None
            orch._resolve_provider_blocked_service=normal_wait
            consumer=AssuranceReviewConsumer(commit,tenant_id=world.mission.tenant_id,principal_id='fixture-current-consumer',authority=builder_authority,cas=cas,check_adapter=None)
            runner=AssuranceReviewRuntime(orch,consumer)
            runner.install()
            orch._assurance_tick=ReviewPump(consumer)
            verdict=await orch._run_critic(world.mission,task,view_id=stored.envelope.attempt_id,
                subject_prefix=stored.envelope.attempt_id+':critic',account_id='budget:'+task.id,
                artifacts=(artifact,),test_output=None,attempt_id=stored.envelope.attempt_id)
            assert verdict.passed,verdict
            assert runner.provenance(world.mission.id,stored.envelope.attempt_id)['verifier_version']=='assurance-review-reply-v2'
            replay=await orch._run_critic(world.mission,task,view_id=stored.envelope.attempt_id,
                subject_prefix=stored.envelope.attempt_id+':critic',account_id='budget:'+task.id,
                artifacts=(artifact,),test_output=None,attempt_id=stored.envelope.attempt_id)
            assert replay==verdict and provider.calls==2
            assert store.connection.execute('SELECT COUNT(*) FROM assurance_review_invocations').fetchone()[0]==2
            assert store.connection.execute("SELECT COUNT(*) FROM budget_reservations WHERE subject_id LIKE '%:assurance:%' AND state='RESERVED'").fetchone()[0]==0
            counts={'provider_calls':provider.calls,'official_records':store.connection.execute('SELECT COUNT(*) FROM review_records WHERE official=1').fetchone()[0]}
            output={'status':'PASS','scope':'original runner -> malformed raw retained -> one funded format repair -> official -> replay; two actual scripted runtime calls with explicit fixture usage; fixture routing/ACL/lease and single-consumer pump; not full deployment, acceptance, real model or UI','counts':counts}
            sources=['assurance_review_runtime.py','assurance_review_handoff.py','assurance_review_transport.py','assurance_review_consumer.py','assurance_review_import.py','assurance_content_review.py','event_handler.py','commit_service.py','selection_commits.py','assurance_settlement.py','accounting_recovery.py','taskgraph_runtime_imports.py']
            output['source_hashes']={name:hashlib.sha256((SDK/'src/agent_orchestrator/orchestrator'/name).read_bytes()).hexdigest() for name in sources}
            path=(EVIDENCE / ('critic-format-repair-seam-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'))
            path.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'status':'PASS','scope':output['scope'],'evidence':str(path)},ensure_ascii=False))
    asyncio.run(run())
