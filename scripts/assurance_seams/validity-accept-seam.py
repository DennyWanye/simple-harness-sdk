"""Official review -> current UseCertificate -> assured scoped acceptance; fixture Mission.

One continuous coding seam for BW08/BW10 (ACCEPT purpose, TASK_CONTENT). Real
Store/Commit/Scope/HtnStore, actual AgentRuntime + ScriptedProvider, original
runner/collector/REVIEW WorkStore/official importer, then the new validity
evaluator and the original acceptance writer. Routing, ACL, lease and the
single-consumer pump are explicit fixtures; no real model, four-consumer
deployment, executor checks, Host or UI.
"""
from seam_paths import SDK, EVIDENCE
import asyncio
import dataclasses
import hashlib
import json
import sys

import jsonschema
from referencing import Registry, Resource
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace
from unittest.mock import patch

sys.path[:0] = [str(SDK / 'tests/orchestrator/full_target'),
                str(SDK / 'tests/orchestrator/full_target/operation_completion'),
                str(SDK / 'tests/agents'), str(Path(__file__).parent)]
import test_plan_commits as plans
import test_completion_spec_approval as approval
from test_scoped_content_commit import _mixed_world
from provider_fixture import MODEL, ScriptedProvider
from simple_harness.agents import build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.providers.base import ProviderUsage
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import AssuranceError, canonical, decode, fingerprint
from agent_orchestrator.assurance.evidence import ReadItem
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.assurance.reviews import REVIEW_CODEC_VERSION
from agent_orchestrator.assurance.root_gate import AssuranceRootGate, CurrentReadPermission
from agent_orchestrator.contracts.evidence_state import ObservationRecord, QueryCompleteness
from agent_orchestrator.contracts.resolution import (AllExpr, CriterionExpr, EvaluationKind,
                                                     RequirementsRevision)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_factory import AssuranceMissionFactory
from agent_orchestrator.orchestrator.assurance_review_consumer import AssuranceReviewConsumer
from agent_orchestrator.orchestrator.assurance_review_runtime import AssuranceReviewRuntime
from agent_orchestrator.orchestrator.assurance_tick import PreparedAssuranceWork
from agent_orchestrator.orchestrator.assurance_validity import AssuranceValidity, acceptance_id_for
from agent_orchestrator.orchestrator.commit_service import CommitService, Reservation
from agent_orchestrator.orchestrator.completion_inputs import load_completion_result_inputs
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly
from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected
from agent_orchestrator.runtime.agent_worker import AgentBridge
from agent_orchestrator.storage.assurance_work import CONSUMERS, AssuranceWorkStore
from agent_orchestrator.storage.htn_store import HtnStore


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


class FixtureCommit(CommitService):
    def __init__(self, store, **kwargs):
        super().__init__(store, **kwargs)
        self._assurance_root_gate = AssuranceRootGate(store, store.path.parent)
        store._assurance_root_gate = self._assurance_root_gate
        self.install_assurance_root(principal=Principal('fixture-authenticated-user'),
                                    tenant_id='tenant-p23a', command_id='install')
        self._assurance_factory = AssuranceMissionFactory(self, tenant_id='tenant-p23a', policy=AssurancePolicy(),
            require_creation_root=self._assurance_root_gate.require_execution, requirements=fixture_requirements,
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


def build_world(root):
    with patch.object(plans, 'CommitService', FixtureCommit), \
         patch.object(approval, '_bind_new_protocol', lambda _: None), \
         patch.object(approval, '_requirements', lambda w: HtnStore(w.store).get_requirements_revision(w.mission.id, 1)):
        world, req, _, task, stored, artifact = _mixed_world(root, accept_result=False, with_output=True)
    store, commit = world.store, world.service
    scope_row = store.connection.execute('SELECT * FROM operation_completion_scopes WHERE mission_id=?', (world.mission.id,)).fetchone()
    scope_ref = AssuranceRef('completion_scope', Pin(scope_row['scope_id'], 0, scope_row['scope_hash']))
    commit.approve_assurance_check_policy(tenant_id=world.mission.tenant_id, mission_id=world.mission.id,
        command_id='fixture-policy-approval', principal=Principal('fixture-authenticated-user'),
        requirements_ref=AssuranceRef('requirements', Pin(str(req.revision_id), req.revision, req.content_hash())),
        completion_scope=scope_ref, candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
    return world, task, stored, artifact, scope_ref


class ReviewPump:
    # Explicit single-consumer fixture: not the four-consumer deployment.
    def __init__(self, store, mission_id, consumer):
        self.store, self.mission_id = store, mission_id
        self.consumers = {'REVIEW': consumer}
        self.work = AssuranceWorkStore(store)

    async def tick(self):
        store = self.store
        cursor = store.connection.execute("SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer='REVIEW'", (self.mission_id,)).fetchone()
        consumer = self.consumers['REVIEW']
        self.work.ingest(self.mission_id, 'REVIEW', expected_version=cursor['row_version'], classify=lambda e, c: consumer.classify(e), now_ms=int(store.now * 1000))
        for claim in self.work.claim_due(self.mission_id, 'REVIEW', owner='runner-fixture', now_ms=int(store.now * 1000), lease_ms=30000, limit=1):
            prepared = await consumer.prepare(claim)
            assert isinstance(prepared, PreparedAssuranceWork), prepared
            self.work.commit(claim, now_ms=int(store.now * 1000), effect=prepared.commit, rejected=prepared.rejected)


async def run_review(root, response, authority_state):
    world, task, stored, artifact, scope_ref = build_world(root)
    store, commit = world.store, world.service
    cas = ArtifactStore(root / 'scoped-cas')
    deadline = int(store.now * 1000) + 60000

    def authority(identity, ref):
        return CurrentReadPermission(
            ReadItem('ACCESS', ref.key, fingerprint({'fixture-principal': identity.principal_id, 'purpose': identity.purpose,
                                                     'ref': ref.to_json(), 'state': authority_state[0]})),
            ReadItem('POLICY', 'fixture-current-policy', 'f' * 64), deadline)

    provider = KnownUsageProvider([canonical(response)])
    async with build_agent_runtime(AgentRuntimePorts(provider=provider, authorization=AllowAllAuthorization(),
                                                     database_path=str(root / 'runtime.db'), model=MODEL, owner_id='runner-seam')) as runtime:
        bridge = AgentBridge(runtime, unpriced=True)
        orch = SimpleNamespace(store=store, commit=commit, bridge_for=lambda _: bridge,
            assembled=SimpleNamespace(workspaces=SimpleNamespace(artifact_store=cas), pool=lambda _: SimpleNamespace(bridge=bridge), gateway=SimpleNamespace(unbind=lambda _: None)),
            _expected_model=lambda _: MODEL, _note=lambda _: None, _assurance_reviews=None,
            _owner='runner-fixture', _poll=0.001, _critic_wait=10,
            _config=SimpleNamespace(lease_seconds=60, turn_deadline_seconds=10, critic_reserve_tokens=100),
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
                     '_settle_intent', '_import_usage', '_service_agent_ids', '_settle_service_if_known', 'profile_of'):
            setattr(orch, name, MethodType(getattr(Orchestrator, name), orch))

        async def normal_wait(intent, liveness):
            assert not Orchestrator._provider_blocked(liveness), 'fixture unexpectedly blocked'
            return None
        orch._resolve_provider_blocked_service = normal_wait
        consumer = AssuranceReviewConsumer(commit, tenant_id=world.mission.tenant_id, principal_id='fixture-current-consumer',
                                           authority=authority, cas=cas, check_adapter=None)
        runner = AssuranceReviewRuntime(orch, consumer)
        runner.install()
        validity = AssuranceValidity(commit, tenant_id=world.mission.tenant_id, principal_id='fixture-current-consumer',
                                     cas=cas, check_adapter=None, authority=authority)
        orch._assurance_tick = ReviewPump(store, world.mission.id, consumer)
        verdict = await orch._run_critic(world.mission, task, view_id=stored.envelope.attempt_id,
            subject_prefix=stored.envelope.attempt_id + ':critic', account_id='budget:' + task.id,
            artifacts=(artifact,), test_output=None, attempt_id=stored.envelope.attempt_id)
        with store.read_view():
            record = runner.task_record(world.mission.id, stored.envelope.attempt_id)
        assert record is not None and provider.calls == 1
        return world, task, stored, artifact, validity, verdict, record


def accept_now(world, task, stored, artifact):
    # The production writer: CommitService.accept_result marks the result DONE/PASS and
    # runs LeafAcceptanceAssembly.accept in that same transaction.
    return world.service.accept_result(stored.envelope.id, verifier_results=())


def replay_accept(world, task, stored, artifact):
    # Exact core command replay (as test_occ11 does); idempotent, never a second writer.
    store, commit = world.store, world.service
    attempt = store.get_attempt(stored.envelope.attempt_id)
    frozen = load_completion_result_inputs(store, store.get_result(stored.envelope.id))
    with store.transaction():
        return LeafAcceptanceAssembly(store, commit).accept(
            world.mission.id, task.id, result_id=stored.envelope.id,
            layers=store.list_verifications(stored.envelope.id), artifacts=(artifact,),
            producer_agent_ids=(attempt.agent_id,), reviewer_agent_id=f'critic:{attempt.id}',
            now_ms=int(store.now * 1000), input_manifest_hash=frozen.frozen.manifest_hash,
            port_claims=frozen.port_claims)


def count(store, sql, *params):
    return store.connection.execute(sql, params).fetchone()[0]


async def main():
    report = {'scope': 'official TASK_CONTENT review -> current ACCEPT UseCertificate (fresh anchors, bounded '
                       'supported/clean closure, complete query sets, current authority) -> original scoped '
                       'acceptance committing the certificate beside the Acceptance; SEMANTIC criterion, no '
                       'consumed executor/local checks; fixture routing/ACL/lease/single-consumer pump; '
                       'not a real model, four-consumer deployment, other purposes, Host or UI'}
    accept_reply = {'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'PASS',
                    'evidence_ids': [], 'reason': 'fixture response', 'limitations': []}], 'findings': []}
    reject_reply = {'schema_version': 2, 'verdict': 'REJECTED', 'assessments': [{'criterion_id': 'criterion-report', 'verdict': 'FAIL',
                    'evidence_ids': [], 'reason': 'fixture refusal', 'limitations': []}], 'findings': []}
    with TemporaryDirectory(prefix='assurance-validity-accept-') as temp:
        root = Path(temp).resolve()
        authority_state = ['current']
        world, task, stored, artifact, validity, verdict, record = await run_review(root, accept_reply, authority_state)
        store = world.store
        mission_id = world.mission.id
        assert verdict.passed, verdict
        # The public V1 projection hides SEMANTIC PASS; the candidate carries the real grade.
        legacy = {o.criterion_id: (o.verdict.value, o.check_execution.value, tuple(o.limitations)) for o in record.criteria}
        assert legacy['criterion-report'][0] == 'UNKNOWN' and 'ASSURANCE_SEMANTIC_GRADE_IN_BOUND_MANIFEST' in legacy['criterion-report'][2], legacy
        # The runtime no longer prepares on PASS (layers recorded afterwards move the
        # epoch); the acceptance path prepares its own. Here the seam prepares once to
        # inspect the certificate and to drive the counter-cases.
        assert validity.candidate_for(mission_id, str(record.record_id)) is None
        candidate = validity.prepare_accept_use_for_result(mission_id, stored.envelope.id)
        assert candidate.usable, candidate
        certificate = candidate.certificate
        assert certificate.truth == 'TRUE' and certificate.decision == 'USABLE' and certificate.purpose == 'ACCEPT'
        assert {r.channel for r in certificate.read_set} == {'OBJECT', 'QUERY_SET', 'ACCESS', 'POLICY'}
        assert candidate.effective_grades == {'criterion-report': 'PASS'}, candidate.effective_grades
        assert candidate.evaluation.clean_support_refs and candidate.evaluation.admitted_anchor_ids
        report['certificate'] = {'decision': certificate.decision, 'truth': certificate.truth, 'read_items': len(certificate.read_set),
                                 'query_sets': sum(1 for r in certificate.read_set if r.channel == 'QUERY_SET'),
                                 'clean_support': [r.to_json() for r in certificate.clean_support_refs],
                                 'reasons': list(certificate.reasons), 'not_after_ms': certificate.not_after_ms,
                                 'issued_at_ms': certificate.issued_at_ms}
        # Preparation is read only.
        before = store.connection.total_changes
        again = validity.prepare_accept_use(record)
        assert store.connection.total_changes == before, 'preparation must not write'
        assert again.certificate.read_set == certificate.read_set
        certificates_sql = 'SELECT COUNT(*) FROM assurance_use_certificates'
        witnesses_sql = "SELECT COUNT(*) FROM validity_witnesses WHERE mission_id=? AND purpose='ACCEPT'"
        witnesses_before = count(store, witnesses_sql, mission_id)
        # Counter 1: identity swap is refused at the final lock.
        swapped = dataclasses.replace(again, identity=dataclasses.replace(again.identity, consumer_id='other-acceptance'))
        with store.transaction():
            report['identity_swap'] = refused(lambda: validity.commit_use_locked(swapped, now_ms=int(store.now * 1000)),
                                              {'CERTIFICATE_USE_IDENTITY'})
        # Counter 2: half-open expiry.
        with store.transaction():
            report['expiry'] = refused(lambda: validity.commit_use_locked(again, now_ms=certificate.not_after_ms),
                                       {'CHECK_USE_EXPIRED', 'CERTIFICATE_EXPIRED'})
        # Counter 3: current authority changed since preparation.
        authority_state[0] = 'revoked'
        with store.transaction():
            report['authority_change'] = refused(lambda: validity.commit_use_locked(again, now_ms=int(store.now * 1000)),
                                                 {'RECHECK_REQUIRED'})
        authority_state[0] = 'current'
        # Counter 4: an unreferenced new source (counter-evidence branch) moves the
        # mission epoch through the same-transaction barrier; the old candidate is stale.
        HtnStore(store).insert_observation(mission_id, ObservationRecord(
            observation_id='seam-counter-observation', proposition_key='fixture.unrelated#1', polarity=False,
            source_ref=TypedRef(kind=TypedRefKind.SOURCE, id='seam-source', revision=1, content_hash='c' * 64),
            observed_at_ms=int(store.now * 1000), recorded_at_ms=int(store.now * 1000),
            coverage=QueryCompleteness.BEST_EFFORT, observer_id='seam-observer'))
        with store.transaction():
            report['source_change'] = refused(lambda: validity.commit_use_locked(again, now_ms=int(store.now * 1000)),
                                              {'RECHECK_REQUIRED'})
        fresh = validity.prepare_accept_use(record)
        assert fresh.usable and fresh.epochs != again.epochs
        rejected = [r for r in fresh.evaluation.rejected_anchors if r[0] == 'seam-counter-observation']
        assert rejected and rejected[0][1] == 'UNREGISTERED_PREDICATE', fresh.evaluation.rejected_anchors
        # Counter 5: a stored observation impersonating the system observer/predicate
        # is rejected and never becomes an anchor.
        from agent_orchestrator.knowledge.assurance_sources import ASSURANCE_OBSERVER, review_accepted_key
        HtnStore(store).insert_observation(mission_id, ObservationRecord(
            observation_id='seam-impersonation', proposition_key=review_accepted_key(mission_id, str(record.record_id)),
            polarity=True, source_ref=TypedRef(kind=TypedRefKind.SOURCE, id='seam-forged', revision=1, content_hash='d' * 64),
            observed_at_ms=int(store.now * 1000), recorded_at_ms=int(store.now * 1000),
            coverage=QueryCompleteness.BEST_EFFORT, observer_id=ASSURANCE_OBSERVER))
        fresh = validity.prepare_accept_use(record)
        forged = [r for r in fresh.evaluation.rejected_anchors if r[0] == 'seam-impersonation']
        assert forged == [('seam-impersonation', 'SYSTEM_PREDICATE_IMPERSONATION')], fresh.evaluation.rejected_anchors
        assert 'seam-impersonation' not in fresh.evaluation.admitted_anchor_ids
        report['impersonation'] = forged[0][1]
        # Counter 6: no validity evaluator bound -> the original acceptance writer refuses,
        # nothing is written; a stale in-memory candidate is not a licence either.
        validity.forget(mission_id, str(record.record_id))
        world.service._assurance_validity = None
        report['missing_candidate'] = refused(lambda: accept_now(world, task, stored, artifact), {'USE_CERTIFICATE_REQUIRED'})
        world.service._assurance_validity = validity
        assert count(store, certificates_sql) == 0 and count(store, 'SELECT COUNT(*) FROM acceptances WHERE mission_id=?', mission_id) == 0
        # As the production router does after _run_critic: record the critic layer with
        # its official provenance. verifications is an inventoried source, so the mission
        # epoch moves here; the acceptance path must prepare after this, not before.
        epoch_before = validity.prepare_accept_use(record).epochs.mission
        world.service.record_verification_layer(stored.envelope.id, layer='critic_review', status='PASS',
            detail={'summary': 'assurance official', 'official_review_record_id': str(record.record_id),
                    'evidence_manifest_hash': record.evidence_manifest_hash, 'verifier_version': REVIEW_CODEC_VERSION})
        validity.forget(mission_id, str(record.record_id))
        report['critic_layer_epoch'] = {'before': epoch_before, 'after': validity.prepare_accept_use(record).epochs.mission}
        assert report['critic_layer_epoch']['after'] > epoch_before
        validity.forget(mission_id, str(record.record_id))
        # The fixture Worker never ran in the AgentRuntime, so its executor cannot be
        # closed by the assured settlement reader. Production defers that settlement
        # to the usage import path when a call's price is still unknown; the seam
        # takes exactly that branch (imported_usage.unknown=1) and does not claim
        # to cover Worker settlement.
        world.service.import_usage(stored.envelope.attempt_id, mission_id,
                                   (UsageFact('fixture-worker-usage', 10, 10, None, unknown=True),))
        report['worker_settlement'] = 'DEFERRED_UNKNOWN_USAGE (fixture worker; not covered here)'
        # The real path: accept_result prepares the current use itself, locks it at
        # the head of its UoW, and commits the certificate beside the Acceptance.
        completed = accept_now(world, task, stored, artifact)
        assert completed.accepted_result_id == stored.envelope.id, completed
        acceptance_id = acceptance_id_for(task.id, stored.envelope.id)
        assert count(store, certificates_sql) == 1
        row = store.connection.execute('SELECT * FROM assurance_use_certificates').fetchone()
        stored_certificate = decode(row['certificate_json'])
        assert stored_certificate['decision'] == 'USABLE' and row['consumer_id'] == acceptance_id, dict(row)
        schemas = SDK / 'src/agent_orchestrator/assurance/schemas'
        common = json.loads((schemas / 'common.schema.json').read_text())
        registry = Registry().with_resource(common['$id'], Resource.from_contents(common))
        jsonschema.Draft202012Validator(
            json.loads((schemas / 'use-certificate-v2.schema.json').read_text()), registry=registry
        ).validate(stored_certificate)
        report['certificate']['schema'] = 'use-certificate-v2.schema.json OK'
        assert validity.candidate_for(mission_id, str(record.record_id)) is None, 'consumed candidate must be forgotten'
        assert count(store, "SELECT COUNT(*) FROM commit_receipts WHERE kind='AssuranceUseCertified'") == 1
        assert store.count_events(mission_id, 'AssuranceUseCertified') == 1
        assert count(store, witnesses_sql, mission_id) == witnesses_before, 'legacy ACCEPT witness must not be minted'
        payload = store.connection.execute(
            "SELECT payload_json FROM events WHERE mission_id=? AND type='AcceptanceCommitted'", (mission_id,)).fetchone()
        assert decode(payload[0])['witness_id'] == row['certificate_id']
        assert count(store, 'SELECT COUNT(*) FROM acceptances WHERE mission_id=?', mission_id) == 1
        assert count(store, 'SELECT COUNT(*) FROM operation_acceptance_scopes WHERE mission_id=?', mission_id) == 1
        # Replay: same command, same receipt, no second certificate.
        replay = replay_accept(world, task, stored, artifact)
        assert replay.replayed and replay.acceptance_id == acceptance_id
        assert count(store, certificates_sql) == 1
        report['acceptance'] = {'acceptance_id': acceptance_id, 'certificate_id': row['certificate_id'],
                                'certificates': 1, 'legacy_witness_minted': False, 'replay_idempotent': True}
        store.close()
    with TemporaryDirectory(prefix='assurance-validity-reject-') as temp:
        root = Path(temp).resolve()
        world, task, stored, artifact, validity, verdict, record = await run_review(root, reject_reply, ['current'])
        store = world.store
        assert not verdict.passed
        assert validity.candidate_for(world.mission.id, str(record.record_id)) is None
        candidate = validity.prepare_accept_use_for_result(world.mission.id, stored.envelope.id)
        assert candidate.certificate.decision != 'USABLE' and candidate.effective_grades == {'criterion-report': 'FAIL'}
        with store.transaction():
            report['rejected_review'] = {'decision': candidate.certificate.decision, 'truth': candidate.certificate.truth,
                                         'commit': refused(lambda: validity.commit_use_locked(candidate, now_ms=int(store.now * 1000)),
                                                           {'CERTIFICATE_NOT_USABLE'})}
        report['rejected_review']['accept'] = refused(lambda: accept_now(world, task, stored, artifact), {'CERTIFICATE_NOT_USABLE'})
        assert count(store, 'SELECT COUNT(*) FROM assurance_use_certificates') == 0
        assert count(store, 'SELECT COUNT(*) FROM acceptances WHERE mission_id=?', world.mission.id) == 0
        store.close()
    report['status'] = 'PASS'
    sources = ['knowledge/assurance_sources.py', 'orchestrator/assurance_validity.py', 'orchestrator/resolution_commits.py',
               'orchestrator/leaf_acceptance.py', 'orchestrator/assurance_review_runtime.py', 'verification/scoped_acceptance.py',
               'verification/acceptance_rules.py', 'assurance/grounding.py', 'assurance/certificates.py',
               'orchestrator/assurance_check_use.py', 'storage/assurance_reads.py', 'storage/assurance_store.py']
    report['source_sha256'] = {name: hashlib.sha256((SDK / 'src/agent_orchestrator' / name).read_bytes()).hexdigest() for name in sources}
    out = EVIDENCE / ('validity-accept-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out), 'certificate': report['certificate']['decision'],
                      'counters': {k: report[k] for k in ('identity_swap', 'expiry', 'authority_change', 'source_change', 'impersonation', 'missing_candidate')}, 'critic_layer_epoch': report['critic_layer_epoch'],
                      'rejected_review': report['rejected_review']}, ensure_ascii=False))


asyncio.run(main())
