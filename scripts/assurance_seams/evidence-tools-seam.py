"""Read-only reviewer evidence tools -> actual Provider input manifest -> labelled exposure batch.

One coding seam for BW06 (handoff §4 item 5). Real Store/Commit/Scope/HtnStore,
actual AgentRuntime + scripted tool-calling Provider through the ORIGINAL
WorkspaceToolGateway, the original collector, REVIEW WorkStore and official
importer. Exercised: `_bind_critic` assured branch binds exactly the two evidence
tools (no Attempt workspace, none fabricated for the root review); find/read go
through the gateway's identity/permission/schema/budget/refusal/audit pipeline;
current-authority permission, review-key pin and byte-exact read before content
is returned; a complete UTF-8 read that the final actual Provider request really
contained becomes disclosure batch 1 and its label is citable; a partial page,
a listing, a binary refusal and an unknown label disclose nothing; replaying the
collector adds no batch. Routing, ACL, lease and the single-consumer pump are
fixtures; no real model, four-consumer deployment, Host or UI.
"""
from _assured_fixture import (EVIDENCE, SDK, TENANT, AssuredRuntime, KnownUsageProvider, count, requirements_ref,
                              source_sha256)
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from simple_harness import MessageRole
from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import decode, fingerprint
from agent_orchestrator.assurance.evidence import evidence_label
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts.resolution import ReviewVerdict
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_review_collect import collect_assurance_review
from agent_orchestrator.orchestrator.assurance_review_import import read_imported_review_locked
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.root_review import RootReviewCoordinator, RootReviewStatus
from agent_orchestrator.runtime.tool_gateway import ASSURANCE_EVIDENCE_TOOLS
from agent_orchestrator.storage.htn_store import HtnStore

EXTRA_PATH, EXTRA_BODY = 'notes/extra.md', b'# extra evidence\nfixture note not in the frozen catalogue\n'
BINARY_PATH, BINARY_BODY = 'bin/blob.dat', b'\xff\xfe\x00\x01binary'
UNKNOWN_LABEL = 'ev-' + '0' * 64


class ToolScriptProvider(KnownUsageProvider):
    """Script steps may be callables of the actual request (to read tool results back)."""
    async def invoke(self, request, *, cancel):
        if self.script and callable(self.script[0]):
            self.script[0] = self.script[0](request)
        return await super().invoke(request, cancel=cancel)


def tool_values(request, name):
    """Successful tool-result values of ``name`` in this actual request, in order."""
    values = []
    for message in request.messages:
        if message.role is MessageRole.TOOL and message.name == name:
            payload = json.loads(message.content)
            if payload['outcome'] == 'succeeded':
                values.append(payload['value'])
    return values


def find_label(request, ref_id):
    for listing in tool_values(request, 'assurance_find_evidence'):
        for entry in listing['entries']:
            if entry['id'] == ref_id:
                return entry['label']
    raise AssertionError(f'{ref_id} not listed: ' + json.dumps(tool_values(request, "assurance_find_evidence")))


def read_label(request):
    reads = tool_values(request, 'assurance_read_evidence')
    assert reads, 'no successful read in the request'
    return reads[-1]['label']


def reply(criteria, label):
    return json.dumps({'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [
        {'criterion_id': c, 'verdict': 'PASS', 'evidence_ids': [label], 'reason': 'cites appended evidence',
         'limitations': []} for c in criteria], 'findings': []}, ensure_ascii=False)


def register_sources(rt):
    store = rt.store
    refs = {}
    for path, body in ((EXTRA_PATH, EXTRA_BODY), (BINARY_PATH, BINARY_BODY)):
        digest = rt.cas.put_bytes(body)
        assert digest == hashlib.sha256(body).hexdigest()
        store.put_source({'mission_id': rt.mission.id, 'tenant_id': TENANT, 'path': path, 'version_hash': digest,
                          'kind': 'note', 'trust': 'untrusted_external', 'registered_at': store.now,
                          'superseded_by': None, 'revoked': False, 'revision': 1})
        refs[path] = AssuranceRef('source', Pin(path, 1, digest))
    return refs


def review_key_of(rt, purpose):
    row = rt.store.connection.execute(
        "SELECT review_key FROM assurance_review_bindings WHERE mission_id=? AND json_extract(binding_json,'$.subject.purpose')=?",
        (rt.mission.id, purpose)).fetchone()
    assert row is not None, purpose
    return row['review_key']


def batches(rt, review_key):
    return [decode(r['batch_json']) for r in rt.store.connection.execute(
        'SELECT batch_json FROM assurance_disclosure_batches WHERE review_key=? ORDER BY batch_no', (review_key,))]


def gateway_records(rt):
    return [{'tool': r['tool'], 'outcome': r.get('outcome'), 'error_code': r.get('error_code'), 'view': r['view'],
             'attempt_id': r['attempt_id'], 'review_key': r.get('review_key')} for r in rt.gateway.calls]


async def task_content_complete(root, report):
    """find -> binary refused -> unknown label refused -> complete read -> verdict cites it."""
    script = [('assurance_find_evidence', {'query': 'source'}),
              lambda req: ('assurance_read_evidence', {'label': find_label(req, BINARY_PATH)}),
              ('assurance_read_evidence', {'label': UNKNOWN_LABEL}),
              lambda req: ('assurance_read_evidence', {'label': find_label(req, EXTRA_PATH)}),
              lambda req: reply(['criterion-report'], read_label(req))]
    async with AssuredRuntime(root, [], provider_class=ToolScriptProvider) as rt:
        rt.provider.script[:] = script
        refs = register_sources(rt)
        store = rt.store
        verdict, record = await rt.run_critic()
        assert verdict.passed, verdict
        assert rt.provider.calls == 5, rt.provider.calls
        review_key = review_key_of(rt, 'TASK_CONTENT')
        extra_label = evidence_label(review_key, refs[EXTRA_PATH])
        # Gateway pipeline: bound to exactly the two tools, verify view, real Attempt id
        # for audit attribution only, no workspace touched, refusals audited by code.
        records = gateway_records(rt)
        assert [r['tool'] for r in records] == ['assurance_find_evidence', 'assurance_read_evidence',
                                                'assurance_read_evidence', 'assurance_read_evidence'], records
        assert [r['outcome'] for r in records] == ['succeeded', 'rejected:evidence_refused',
                                                   'rejected:evidence_refused', 'succeeded'], records
        assert records[1]['error_code'] == 'REVIEW_MATERIAL_CODEC_UNSUPPORTED', records[1]
        assert records[2]['error_code'] == 'EVIDENCE_LABEL_UNKNOWN', records[2]
        assert all(r['view'] == 'verify' and r['review_key'] == review_key
                   and r['attempt_id'] == rt.stored.envelope.attempt_id for r in records), records
        assert not list((rt.root / 'seam-workspaces').glob('*')), 'no Attempt workspace may be created'
        # Exposure chain: batch 0 = frozen initial materials, batch 1 = the complete read
        # that the final actual Provider request contained (the listing and the two
        # refusals disclose nothing).
        chain = batches(rt, review_key)
        assert [b['batch_no'] for b in chain] == [0, 1], chain
        assert [e['label'] for e in chain[1]['entries']] == [extra_label], chain[1]
        assert chain[1]['previous_batch_hash'] is not None and len(chain[1]['visible_message_ids']) == 1
        assert chain[0]['provider_input_hash'] == chain[1]['provider_input_hash']
        assert chain[1]['reviewer_agent_id'] == chain[0]['reviewer_agent_id']
        # The pinned read left a live review-key pin for the appended blob.
        pin = store.connection.execute('SELECT state FROM assurance_blob_pins WHERE review_key=? AND blob_hash=?',
                                       (review_key, refs[EXTRA_PATH].pin.content_hash)).fetchone()
        assert pin is not None and pin['state'] in {'PREPARING', 'BOUND'}, pin
        # The official importer consumed the appended label: catalogue ∪ chain, exposed set.
        intent = rt.invocation_intent(review_key)
        with store.read_view():
            record_row = store.connection.execute(
                "SELECT commit_id, receipt_json FROM commit_receipts WHERE kind='AssuranceReviewClassified' AND json_extract(receipt_json,'$.review_key')=?",
                (review_key,)).fetchone()
            classification = decode(record_row['receipt_json'])
            assert classification['classification'] == 'READY_FOR_CURRENT_REVIEW', classification
            from agent_orchestrator.storage.assurance_reads import AssuranceReader
            reader = AssuranceReader(store, tenant_id=TENANT, mission_id=rt.mission.id)
            classification_ref = AssuranceRef('commit_receipt', Pin(record_row['commit_id'], 0, fingerprint(classification)))
            imported = read_imported_review_locked(rt.commit, reader, classification_ref)
            assert extra_label in imported.exposed, sorted(imported.exposed)
            assert any(e.label == extra_label for e in imported.catalogue)
            assert [b.batch_no for b in imported.disclosures] == [0, 1], imported.disclosures
        assert record.verdict is ReviewVerdict.ACCEPT and not rt.pump.rejections, rt.pump.rejections
        # Replaying the original collector on the same turn adds no batch.
        await collect_assurance_review(rt.orch, intent)
        assert [b['batch_no'] for b in batches(rt, review_key)] == [0, 1]
        report['task_content_complete_read'] = {
            'review_key': review_key, 'provider_calls': rt.provider.calls, 'gateway_calls': records,
            'batches': [{'batch_no': b['batch_no'], 'labels': [e['label'] for e in b['entries']],
                         'visible_messages': len(b['visible_message_ids'])} for b in chain],
            'appended_label': extra_label, 'record_id': str(record.record_id), 'verdict': str(record.verdict),
            'replay_added_batch': False}


async def task_content_partial(root, report):
    """A partial page (complete=false) never discloses: citing it is UNEXPOSED_EVIDENCE."""
    script = [('assurance_find_evidence', {}),
              lambda req: ('assurance_read_evidence', {'label': find_label(req, EXTRA_PATH), 'max_chars': 4}),
              lambda req: reply(['criterion-report'], read_label(req))]
    async with AssuredRuntime(root, [], provider_class=ToolScriptProvider) as rt:
        rt.provider.script[:] = script
        register_sources(rt)
        store = rt.store
        try:
            await rt.run_critic()
        except Exception as error:  # the runner reports the durable rejection
            outcome = str(error)
        else:
            raise AssertionError('a partial page must not be citable')
        review_key = review_key_of(rt, 'TASK_CONTENT')
        reads = [json.loads(m.content)['value'] for m in rt.provider.requests[-1].messages
                 if m.role is MessageRole.TOOL and m.name == 'assurance_read_evidence']
        assert reads and reads[0]['complete'] is False and reads[0]['disclosure'] == 'PARTIAL_NOT_CITABLE', reads
        assert reads[0]['next_offset'] == 4 and reads[0]['content'] == EXTRA_BODY.decode()[:4], reads
        assert f"max_chars={len(EXTRA_BODY.decode())}" in reads[0]['complete_read_hint'], reads
        assert [b['batch_no'] for b in batches(rt, review_key)] == [0], 'partial read must not be disclosed'
        reason = store.connection.execute(
            "SELECT json_extract(receipt_json,'$.reason') FROM commit_receipts WHERE kind='AssuranceReviewImportRejected' AND subject_id=?",
            (review_key,)).fetchone()
        assert reason is not None and reason[0] == 'UNEXPOSED_EVIDENCE', (reason, outcome)
        assert 'AssuranceReviewImportRejected' in outcome, outcome
        with store.read_view():
            assert rt.runner.task_record(rt.mission.id, rt.stored.envelope.attempt_id) is None
        report['task_content_partial_read'] = {'review_key': review_key, 'batches': [0], 'rejection': reason[0],
                                               'runner_outcome': outcome, 'page': reads[0]['content']}


async def mission_final_no_attempt(root, report):
    """The root (MISSION_FINAL) reviewer reads through the same tools with no Attempt at all."""
    script = [('assurance_find_evidence', {'query': 'notes/'}),
              lambda req: ('assurance_read_evidence', {'label': find_label(req, EXTRA_PATH)}),
              lambda req: reply(['criterion-report'], read_label(req))]
    leaf_reply = json.dumps({'schema_version': 2, 'verdict': 'ACCEPT', 'assessments': [
        {'criterion_id': 'criterion-report', 'verdict': 'PASS', 'evidence_ids': [], 'reason': 'leaf', 'limitations': []}],
        'findings': []})
    async with AssuredRuntime(root, [], content_only=True, provider_class=ToolScriptProvider) as rt:
        # The root can only be cut once the leaf outcome is accepted (as in the
        # purpose-builders seam); that leaf review uses no tools here.
        rt.provider.script[:] = [leaf_reply]
        verdict, record = await rt.run_critic()
        assert verdict.passed
        rt.record_critic_layer(record)
        rt.settle_fixture_worker()
        assert rt.accept_now().accepted_result_id == rt.stored.envelope.id
        assert not rt.gateway.calls
        rt.provider.script[:] = script
        refs = register_sources(rt)
        store, commit, mission = rt.store, rt.commit, rt.mission
        req = HtnStore(store).get_requirements_revision(mission.id, 1)
        commit.approve_assurance_check_policy(tenant_id=TENANT, mission_id=mission.id,
            command_id='fixture-mission-final-policy', principal=Principal('fixture-authenticated-user'),
            requirements_ref=requirements_ref(req), completion_scope=rt.scope_ref, purpose='MISSION_FINAL',
            candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),))
        dispatch = HierarchicalDispatch(store, commit)
        coordinator = RootReviewCoordinator(store, commit, dispatch, scope_id='mission', issued_by='runner-fixture',
                                            max_cuts_per_revision=2)
        package = coordinator.cut(mission.id, now_ms=int(store.now * 1000))
        asked = await rt.orch._ask_root_reviewer(mission, coordinator, package)
        assert asked is True, rt.notes
        review_key = review_key_of(rt, 'MISSION_FINAL')
        await rt.drive_review(review_key)
        assert rt.provider.calls == 4 and not rt.pump.rejections, (rt.provider.calls, rt.pump.rejections)
        records = gateway_records(rt)
        assert [r['outcome'] for r in records] == ['succeeded', 'succeeded'], records
        assert all(r['attempt_id'] == '' and r['view'] == 'verify' and r['review_key'] == review_key for r in records), records
        assert not list((rt.root / 'seam-workspaces').glob('*')), 'the root review must not fake an Attempt'
        extra_label = evidence_label(review_key, refs[EXTRA_PATH])
        chain = batches(rt, review_key)
        assert [b['batch_no'] for b in chain] == [0, 1] and [e['label'] for e in chain[1]['entries']] == [extra_label], chain
        with store.read_view():
            record = HtnStore(store).official_review_record(str(package.package_id))
            assert record is not None and record.verdict is ReviewVerdict.ACCEPT and str(record.purpose) == 'MISSION_FINAL', record
        assert coordinator.state(mission.id).status is RootReviewStatus.READY
        intent = rt.invocation_intent(review_key)
        assert tuple(intent.config['agent_config']['tool_names']) == ASSURANCE_EVIDENCE_TOOLS
        report['mission_final_no_attempt'] = {'review_key': review_key, 'gateway_calls': records,
                                              'appended_label': extra_label, 'record_id': str(record.record_id),
                                              'root_state': str(coordinator.state(mission.id).status)}


async def main():
    report = {'scope': 'BW06: assured reviewer template carries assurance_find_evidence/assurance_read_evidence; '
                       '_bind_critic assured branch -> original WorkspaceToolGateway pipeline (no Attempt workspace, '
                       'none fabricated for the root review) -> current authority + review-key pin + byte-exact read -> '
                       'complete UTF-8 read in the final actual Provider request -> disclosure batch 1 -> official '
                       'importer cites it; partial page/listing/binary refusal/unknown label disclose nothing; collector '
                       'replay adds no batch. Fixture routing/ACL/lease/single-consumer pump; not a real model, '
                       'four-consumer deployment, Host or UI.'}
    with TemporaryDirectory(prefix='assurance-evidence-tools-') as temp:
        root = Path(temp).resolve()
        await task_content_complete(root / 'complete', report)
        await task_content_partial(root / 'partial', report)
        await mission_final_no_attempt(root / 'final', report)
    report['sources_sha256'] = source_sha256(['verification/reviewer_evidence_tools.py', 'runtime/tool_gateway.py',
                                              'assurance/review_input.py', 'orchestrator/assurance_review_collect.py',
                                              'orchestrator/assurance_review_runtime.py',
                                              'orchestrator/assurance_review_handoff.py',
                                              'orchestrator/event_handler.py'])
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    out = EVIDENCE / 'evidence-tools-seam.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('PASS', out)


if __name__ == '__main__':
    asyncio.run(main())
