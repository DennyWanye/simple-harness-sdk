"""Official review -> current UseCertificate -> assured scoped acceptance; fixture Mission.

One continuous coding seam for BW08/BW10 (ACCEPT purpose, TASK_CONTENT). Real
Store/Commit/Scope/HtnStore, actual AgentRuntime + ScriptedProvider, original
runner/collector/REVIEW WorkStore/official importer, then the new validity
evaluator and the original acceptance writer. Routing, ACL, lease and the
single-consumer pump are explicit fixtures; no real model, four-consumer
deployment, executor checks, Host or UI.
"""
from _assured_fixture import EVIDENCE, SDK, AssuredRuntime, count, refused, source_sha256
import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import jsonschema
from referencing import Registry, Resource
from agent_orchestrator.assurance.codec import decode
from agent_orchestrator.assurance.reviews import REVIEW_CODEC_VERSION
from agent_orchestrator.contracts.evidence_state import ObservationRecord, QueryCompleteness
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.orchestrator.assurance_validity import acceptance_id_for
from agent_orchestrator.orchestrator.completion_inputs import load_completion_result_inputs
from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly
from agent_orchestrator.storage.htn_store import HtnStore


async def run_review(root, response, authority_state):
    """Original Critic entry on the shared fixture; the AgentRuntime closes here, as
    the rest of this seam only exercises store-side validity and acceptance."""
    async with AssuredRuntime(root, [response], authority_state) as rt:
        verdict, record = await rt.run_critic()
        assert rt.provider.calls == 1
    return rt.world, rt.task, rt.stored, rt.artifact, rt.validity, verdict, record


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
    report['source_sha256'] = source_sha256(sources)
    out = EVIDENCE / ('validity-accept-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out), 'certificate': report['certificate']['decision'],
                      'counters': {k: report[k] for k in ('identity_swap', 'expiry', 'authority_change', 'source_change', 'impersonation', 'missing_candidate')}, 'critic_layer_epoch': report['critic_layer_epoch'],
                      'rejected_review': report['rejected_review']}, ensure_ascii=False))


asyncio.run(main())
