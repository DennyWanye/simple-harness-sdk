"""Shared assured-lane fixture for the opt-in seams (not a test, not a deployment).

Real Store/Commit/Scope/HtnStore, actual AgentRuntime + ScriptedProvider, the
original runner/collector/REVIEW WorkStore/official importer, the validity
evaluator and the original acceptance writer. Routing, ACL, lease and the
single-consumer pump are explicit fixtures; no real model, four-consumer
deployment, executor checks, Host or UI. Each seam says what it exercises.
"""
from seam_paths import SDK, EVIDENCE  # noqa: F401  (side effect: sys.path, evidence dir)
import dataclasses
import hashlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

sys.path[:0] = [str(SDK / 'tests/orchestrator/full_target'),
                str(SDK / 'tests/orchestrator/full_target/operation_completion'),
                str(SDK / 'tests/agents'), str(Path(__file__).parent)]
import test_plan_commits as plans  # noqa: E402
import test_completion_spec_approval as approval  # noqa: E402
import test_scoped_content_commit as scoped  # noqa: E402
from test_scoped_content_commit import _mixed_world  # noqa: E402
from provider_fixture import MODEL, ScriptedProvider  # noqa: E402
from simple_harness.agents import build_agent_runtime  # noqa: E402
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization  # noqa: E402
from simple_harness.providers.base import ProviderUsage  # noqa: E402
from agent_orchestrator.artifacts.store import ArtifactStore  # noqa: E402
from agent_orchestrator.assurance.checks import CriterionPolicy  # noqa: E402
from agent_orchestrator.assurance.codec import AssuranceError, canonical, fingerprint  # noqa: E402
from agent_orchestrator.assurance.evidence import ReadItem  # noqa: E402
from agent_orchestrator.assurance.policy import AssurancePolicy  # noqa: E402
from agent_orchestrator.assurance.refs import AssuranceRef, Pin  # noqa: E402
from agent_orchestrator.assurance.reviews import REVIEW_CODEC_VERSION  # noqa: E402
from agent_orchestrator.assurance.root_gate import AssuranceRootGate, CurrentReadPermission  # noqa: E402
from agent_orchestrator.contracts.resolution import (AllExpr, CriterionExpr, EvaluationKind,  # noqa: E402
                                                     RequirementsRevision)
from agent_orchestrator.governance.budgets import UsageFact  # noqa: E402
from agent_orchestrator.governance.permissions import Principal  # noqa: E402
from agent_orchestrator.orchestrator.assurance_factory import AssuranceMissionFactory  # noqa: E402
from agent_orchestrator.orchestrator.assurance_review_collect import collect_assurance_review  # noqa: E402
from agent_orchestrator.orchestrator.assurance_review_consumer import AssuranceReviewConsumer  # noqa: E402
from agent_orchestrator.orchestrator.assurance_review_runtime import AssuranceReviewRuntime  # noqa: E402
from agent_orchestrator.orchestrator.assurance_tick import PreparedAssuranceWork  # noqa: E402
from agent_orchestrator.orchestrator.assurance_validity import AssuranceValidity  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import CommitService, Reservation  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected  # noqa: E402
from agent_orchestrator.runtime.agent_worker import AgentBridge  # noqa: E402
from agent_orchestrator.storage.assurance_work import CONSUMERS, AssuranceWorkStore  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402

TENANT = 'tenant-p23a'
PRINCIPAL = 'fixture-current-consumer'


def requirements(mission, spec):
    return RequirementsRevision(revision_id='completion-requirements-1', mission_id=mission.id, revision=1,
        criteria=(approval._criterion('criterion-report'), approval._criterion('criterion-delivered')),
        success_expression=AllExpr((CriterionExpr('criterion-report'), CriterionExpr('criterion-delivered'))),
        authority_subject='authenticated-user-confirmation')


def fixture_requirements(mission, spec):
    original = requirements(mission, spec)
    return dataclasses.replace(original, criteria=tuple(
        dataclasses.replace(c, evaluation_kind=EvaluationKind.SEMANTIC) if c.criterion_id == 'criterion-report' else c
        for c in original.criteria))


def content_only_requirements(mission, spec):
    """One SEMANTIC content criterion, no required effect: the CONTENT_ONLY Scope
    the root review can be cut over without an operation/effect proof."""
    return RequirementsRevision(revision_id='completion-requirements-1', mission_id=mission.id, revision=1,
        criteria=(dataclasses.replace(approval._criterion('criterion-report'), evaluation_kind=EvaluationKind.SEMANTIC),),
        success_expression=AllExpr((CriterionExpr('criterion-report'),)),
        authority_subject='authenticated-user-confirmation')


def content_only_command(requirements):
    """The user-confirmed completion proposal for a CONTENT_ONLY single root."""
    return {
        'mission_id': requirements.mission_id,
        'command_id': 'confirm-completion-1',
        'expected_requirements_ref': approval._requirements_ref(requirements).to_json(),
        'proposal': {
            'schema_version': 1,
            'mission_id': requirements.mission_id,
            'requirements_ref': {'id': str(requirements.revision_id), 'revision': int(requirements.revision),
                                 'content_hash': requirements.content_hash()},
            'mode': 'CONTENT_ONLY',
            'content_criterion_ids': ['criterion-report'],
            'effects': [],
        },
    }


class FixtureCommit(CommitService):
    REQUIREMENTS = staticmethod(fixture_requirements)

    def __init__(self, store, **kwargs):
        super().__init__(store, **kwargs)
        self._assurance_root_gate = AssuranceRootGate(store, store.path.parent)
        store._assurance_root_gate = self._assurance_root_gate
        self.install_assurance_root(principal=Principal('fixture-authenticated-user'),
                                    tenant_id=TENANT, command_id='install')
        self._assurance_factory = AssuranceMissionFactory(self, tenant_id=TENANT, policy=AssurancePolicy(),
            require_creation_root=self._assurance_root_gate.require_execution, requirements=self.REQUIREMENTS,
            reconcile=lambda _: {consumer: () for consumer in CONSUMERS})

    def create_mission(self, spec, **kwargs):
        return super().create_mission(dataclasses.replace(spec, planning_protocol_version='planning-decision-v1'), **kwargs)


def refused(call, expected):
    try:
        call()
    except AssuranceError as error:
        assert error.code in expected, (error.code, expected)
        return error.code
    except ResolutionCommitRejected as error:
        assert error.reason in expected, (error.reason, expected)
        return error.reason
    raise AssertionError('not refused: ' + str(expected))


class KnownUsageProvider(ScriptedProvider):
    async def invoke(self, request, *, cancel):
        result = await super().invoke(request, cancel=cancel)
        return dataclasses.replace(result, usage=ProviderUsage(10, 10, 20))


def build_world(root, *, content_only=False):
    """Assured Mission with one primitive root, a frozen Scope (MIXED by default,
    CONTENT_ONLY on request), one verified Result and the approved TASK_CONTENT
    policy (criterion-report SEMANTIC)."""
    commit_class = FixtureCommit
    command = scoped._command
    if content_only:
        commit_class = type('ContentOnlyFixtureCommit', (FixtureCommit,), {'REQUIREMENTS': staticmethod(content_only_requirements)})
        command = content_only_command
    with patch.object(plans, 'CommitService', commit_class), \
         patch.object(scoped, '_command', command), \
         patch.object(approval, '_bind_new_protocol', lambda _: None), \
         patch.object(approval, '_requirements', lambda w: HtnStore(w.store).get_requirements_revision(w.mission.id, 1)):
        world, req, _, task, stored, artifact = _mixed_world(root, accept_result=False, with_output=True)
    store, commit = world.store, world.service
    scope_row = store.connection.execute('SELECT * FROM operation_completion_scopes WHERE mission_id=?', (world.mission.id,)).fetchone()
    scope_ref = AssuranceRef('completion_scope', Pin(scope_row['scope_id'], 0, scope_row['scope_hash']))
    commit.approve_assurance_check_policy(tenant_id=world.mission.tenant_id, mission_id=world.mission.id,
        command_id='fixture-policy-approval', principal=Principal('fixture-authenticated-user'),
        requirements_ref=requirements_ref(req),
        completion_scope=scope_ref, candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
    return world, task, stored, artifact, scope_ref


def requirements_ref(req):
    return AssuranceRef('requirements', Pin(str(req.revision_id), req.revision, req.content_hash()))


class ReviewPump:
    # Explicit single-consumer fixture: not the four-consumer deployment.
    def __init__(self, store, mission_id, consumer):
        self.store, self.mission_id = store, mission_id
        self.consumers = {'REVIEW': consumer}
        self.work = AssuranceWorkStore(store)
        self.rejections = []

    async def tick(self):
        store = self.store
        cursor = store.connection.execute("SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer='REVIEW'", (self.mission_id,)).fetchone()
        consumer = self.consumers['REVIEW']
        self.work.ingest(self.mission_id, 'REVIEW', expected_version=cursor['row_version'], classify=lambda e, c: consumer.classify(e), now_ms=int(store.now * 1000))
        for claim in self.work.claim_due(self.mission_id, 'REVIEW', owner='runner-fixture', now_ms=int(store.now * 1000), lease_ms=30000, limit=1):
            prepared = await consumer.prepare(claim)
            assert isinstance(prepared, PreparedAssuranceWork), prepared
            if prepared.rejected:
                self.rejections.append(prepared.rejected)
            self.work.commit(claim, now_ms=int(store.now * 1000), effect=prepared.commit, rejected=prepared.rejected)


class AssuredRuntime:
    """Async context: world + live AgentRuntime + original runner/consumer/validity.

    ``responses`` are scripted model replies consumed in order; append more to
    ``provider.script`` before driving a later review. ``authority_state`` is a
    one-element list the seam may flip to simulate a current-authority change.
    """

    def __init__(self, root, responses, authority_state=None, *, content_only=False):
        self.root = Path(root)
        self.content_only = content_only
        self.responses = list(responses)
        self.authority_state = authority_state if authority_state is not None else ['current']
        self.notes = []

    def authority(self, identity, ref):
        return CurrentReadPermission(
            ReadItem('ACCESS', ref.key, fingerprint({'fixture-principal': identity.principal_id, 'purpose': identity.purpose,
                                                     'ref': ref.to_json(), 'state': self.authority_state[0]})),
            ReadItem('POLICY', 'fixture-current-policy', 'f' * 64), self.deadline_ms)

    async def __aenter__(self):
        self.world, self.task, self.stored, self.artifact, self.scope_ref = build_world(self.root, content_only=self.content_only)
        world = self.world
        self.store, self.commit, self.mission = world.store, world.service, world.mission
        store, commit = self.store, self.commit
        self.cas = ArtifactStore(self.root / 'scoped-cas')
        self.deadline_ms = int(store.now * 1000) + 60000
        self.provider = KnownUsageProvider([canonical(r) for r in self.responses])
        self._runtime_cm = build_agent_runtime(AgentRuntimePorts(provider=self.provider, authorization=AllowAllAuthorization(),
                                                                  database_path=str(self.root / 'runtime.db'), model=MODEL, owner_id='runner-seam'))
        runtime = await self._runtime_cm.__aenter__()
        bridge = AgentBridge(runtime, unpriced=True)
        self.bridge = bridge
        orch = SimpleNamespace(store=store, commit=commit, bridge_for=lambda _: bridge,
            assembled=SimpleNamespace(workspaces=SimpleNamespace(artifact_store=self.cas), pool=lambda _: SimpleNamespace(bridge=bridge), gateway=SimpleNamespace(unbind=lambda _: None)),
            _expected_model=lambda _: MODEL, _note=self.notes.append, _assurance_reviews=None,
            _owner='runner-fixture', _poll=0.001, _critic_wait=10,
            _config=SimpleNamespace(lease_seconds=60, turn_deadline_seconds=10, critic_reserve_tokens=100,
                                    max_root_review_cuts=2),
            _assurance_root_gate=commit._assurance_root_gate, _assurance_management_only=False,
            _route_service=lambda *a: SimpleNamespace(profile_id='fixture-review'),
            _service_config=lambda d: {'runtime_profile_id': d.profile_id, 'model': MODEL},
            _reservation=lambda *a: Reservation(tokens=100, cost_micros=0),
            _assembly_missing=lambda *a, **k: False, _pool_missing=lambda _: False,
            _context_profile_for=lambda _: None, _fault=lambda *a: None,
            _hold_lease=lambda _: None, _service_blocked_since={},
            _validate_mission_judge_intent=lambda _: None)
        for name in ('_run_critic', '_dispatch', '_dispatch_until_submitted', '_await_service_turn',
                     '_critic_subject_stopped', '_bind_critic', '_require_assurance_execution_root',
                     '_settle_intent', '_import_usage', '_service_agent_ids', '_settle_service_if_known', 'profile_of',
                     '_ask_root_reviewer', '_root_review'):
            setattr(orch, name, MethodType(getattr(Orchestrator, name), orch))

        async def normal_wait(intent, liveness):
            assert not Orchestrator._provider_blocked(liveness), 'fixture unexpectedly blocked'
            return None
        orch._resolve_provider_blocked_service = normal_wait
        self.orch = orch
        self.consumer = AssuranceReviewConsumer(commit, tenant_id=TENANT, principal_id=PRINCIPAL,
                                                authority=self.authority, cas=self.cas, check_adapter=None)
        self.runner = AssuranceReviewRuntime(orch, self.consumer)
        self.runner.install()
        self.validity = AssuranceValidity(commit, tenant_id=TENANT, principal_id=PRINCIPAL,
                                          cas=self.cas, check_adapter=None, authority=self.authority)
        self.pump = ReviewPump(store, self.mission.id, self.consumer)
        orch._assurance_tick = self.pump
        return self

    async def __aexit__(self, *exc):
        return await self._runtime_cm.__aexit__(*exc)

    # ------------------------------------------------------------ TASK_CONTENT
    async def run_critic(self):
        """The original Critic entry: routed config, transport, collection, official import."""
        stored = self.stored
        verdict = await self.orch._run_critic(self.mission, self.task, view_id=stored.envelope.attempt_id,
            subject_prefix=stored.envelope.attempt_id + ':critic', account_id='budget:' + self.task.id,
            artifacts=(self.artifact,), test_output=None, attempt_id=stored.envelope.attempt_id)
        with self.store.read_view():
            record = self.runner.task_record(self.mission.id, stored.envelope.attempt_id)
        assert record is not None
        return verdict, record

    def record_critic_layer(self, record, status='PASS'):
        """As the production router does after _run_critic."""
        self.commit.record_verification_layer(self.stored.envelope.id, layer='critic_review', status=status,
            detail={'summary': 'assurance official', 'official_review_record_id': str(record.record_id),
                    'evidence_manifest_hash': record.evidence_manifest_hash, 'verifier_version': REVIEW_CODEC_VERSION})

    def settle_fixture_worker(self):
        """The fixture Worker never ran in the AgentRuntime; production defers its
        settlement to the usage import path when the price is unknown."""
        self.commit.import_usage(self.stored.envelope.attempt_id, self.mission.id,
                                 (UsageFact('fixture-worker-usage', 10, 10, None, unknown=True),))

    def accept_now(self):
        """The production writer: accept_result prepares/locks the current use itself."""
        return self.commit.accept_result(self.stored.envelope.id, verifier_results=())

    # ------------------------------------------------------- other purposes
    def invocation_intent(self, review_key):
        row = self.store.connection.execute(
            'SELECT dispatch_intent_id FROM assurance_review_invocations WHERE mission_id=? AND review_key=? '
            'ORDER BY ordinal DESC LIMIT 1', (self.mission.id, review_key)).fetchone()
        return None if row is None else self.store.get_intent(row[0])

    async def drive_review(self, review_key, *, ticks=3):
        """Dispatch the transport intent, wait for its turn, collect it, pump REVIEW."""
        intent = self.invocation_intent(review_key)
        assert intent is not None, review_key
        deadline = self.store.now + 30
        notes_before = len(self.notes)
        try:
            intent, answer = await self.orch._await_service_turn(intent, deadline, attempt_id=None)
        except Exception as error:
            current = self.store.get_intent(intent.intent_id)
            raise AssertionError({'error': str(error), 'intent_state': current.state, 'intent_kind': current.kind,
                                  'notes': self.notes[notes_before:][-5:]}) from error
        assert answer is not None, 'reviewer did not answer'
        await collect_assurance_review(self.orch, intent)
        for _ in range(ticks):
            await self.pump.tick()
        return intent


def count(store, sql, *params):
    return store.connection.execute(sql, params).fetchone()[0]


def source_sha256(names):
    return {name: hashlib.sha256((SDK / 'src/agent_orchestrator' / name).read_bytes()).hexdigest() for name in names}
