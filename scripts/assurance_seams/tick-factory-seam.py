"""Narrow coding seam with fixture authority/consumers, NOT product acceptance."""
from seam_paths import SDK, EVIDENCE
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent_orchestrator.assurance.codec import AssuranceError, fingerprint
from agent_orchestrator.assurance.grounding import compute_grounded_support
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts.evidence_state import ObservationRecord, WitnessPurpose, QueryCompleteness
from agent_orchestrator.contracts.resolution import RequirementsRevision, Criterion, CriterionExpr
from agent_orchestrator.contracts.semantic_base import TypedRef, VersionedRef
from agent_orchestrator.knowledge.justifications import AnchorCandidate, AnchorSelector, Atom, JustificationSet, SupportGraph
from agent_orchestrator.knowledge.predicates import PredicateSignature
from agent_orchestrator.orchestrator.assurance_factory import AssuranceMissionFactory
from agent_orchestrator.orchestrator.assurance_tick import AssuranceTick, PreparedAssuranceWork
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.storage.assurance_reads import AssuranceReader, read_complete_evidence_snapshot
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.assurance_work import CONSUMERS, WorkTarget
from agent_orchestrator.storage.store import Store


def fixture_requirements(mission, spec):
    return RequirementsRevision(
        revision_id='req:' + mission.id, mission_id=mission.id, revision=1,
        criteria=(Criterion(criterion_id='c', revision=1, origin='USER_EXPLICIT',
            statement='coding fixture only', requirement_class='REQUIRED_OUTCOME', evaluation_kind='SEMANTIC'),),
        success_expression=CriterionExpr('c'),
    )


async def tick_seam():
    wall = [100.0]
    with TemporaryDirectory(prefix='assurance-tick-seam-') as root:
        store = Store.open(Path(root)/'orchestrator.db', clock=lambda: wall[0])
        commit = CommitService(store)
        body = {'root_incarnation_id':'root-fixture'}
        store.insert_receipt(commit_id='install', kind='AssuranceEnvironmentInstalled',
            subject_id='root-fixture', base_version=None, proposal_hash=fingerprint(body), receipt=body)
        AssuranceStore(store).initialize_environment(
            receipt=AssuranceRef('commit_receipt', Pin('install', 0, fingerprint(body))),
            root_incarnation_id='root-fixture', now_ms=100000)
        targets = {c: () for c in CONSUMERS}
        targets['VALIDITY'] = (WorkTarget('validity-fixture', 'a'*64),)
        commit._assurance_factory = AssuranceMissionFactory(commit, tenant_id='tenant', policy=AssurancePolicy(),
            require_creation_root=lambda: None, requirements=fixture_requirements,
            reconcile=lambda mission_id: targets)
        mission, created = commit.create_mission(MissionSpec(goal='fixture', success_criteria=('c',),
            tenant_id='tenant', idempotency_key='fixture', orchestration_semantics_version='hierarchical',
            planning_protocol_version='planning-decision-v1'))
        assert created and AssuranceStore(store).lane(mission.id) == 'ASSURANCE_1_1'
        activation = next(e for e in store.list_events(mission.id) if e.type == 'AssuranceProfileActivated')
        assert {r[0] for r in store.connection.execute('SELECT last_event_seq FROM assurance_event_cursors')} == {activation.seq}
        # The source bridge itself invalidates the snapshot, without recursively
        # treating its own wakeup as another source.
        imported = commit._emit('AssuranceLocalCheckFinished', mission.id, key='fixture-check', payload={'fixture': True})
        ref = AssuranceRef('local_check_receipt', Pin(imported.id, 0, fingerprint(imported.to_json())))
        reader = AssuranceReader(store, tenant_id='tenant', mission_id=mission.id)
        assert reader.read_exact_metadata(ref).ref == ref
        try:
            reader.read_exact_metadata(AssuranceRef('local_check_receipt', Pin(imported.id, 0, fingerprint(dict(imported.payload)))))
        except AssuranceError as error:
            assert error.code == 'REF_BODY_CONFLICT'
        else:
            raise AssertionError('payload-only event ref accepted')
        assert store.count_events(mission.id, 'AssuranceEvidenceChanged') == 1
        assert any('AssuranceLocalCheckFinished' in row for q in read_complete_evidence_snapshot(reader,scope_id='mission') for row in q.rows)
        try:
            with store.transaction() as connection:
                connection.execute('UPDATE events SET type=? WHERE event_id=?', ('unrelated', imported.id))
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('source bridge rewrite accepted')

        class Adapter:
            count = 0
            def classify(self, event):
                return ()
            async def prepare(self, claim):
                self.count += 1
                if self.count == 1:
                    wall[0] = 90.0
                    await asyncio.sleep(0.1)
                def original_effect():
                    receipt = {'fixture':True,'mission_id':mission.id}
                    store.insert_receipt(commit_id='fixture-effect',kind='FixtureOnly',subject_id=mission.id,
                        base_version=None,proposal_hash=fingerprint(receipt),receipt=receipt)
                    return AssuranceRef('commit_receipt',Pin('fixture-effect',0,fingerprint(receipt)))
                return PreparedAssuranceWork(original_effect)

        adapter = Adapter()
        tick = AssuranceTick(SimpleNamespace(store=store,commit=commit,_owner='fixture-owner'),
            consumers={c:adapter for c in CONSUMERS}, root_incarnation_id='root-fixture',
            require_execution_root=lambda:None, tenant_id='tenant', prepare_timeout_ms=10,prepare_lease_ms=2000)
        await tick.tick()
        row = store.connection.execute('SELECT * FROM assurance_pending_work').fetchone()
        assert row['state'] == 'WAITING' and row['wait_reason'] == 'TIME_DISCONTINUITY'
        assert row['not_before_ms'] == 100000 and row['tries'] == 1
        await tick.tick()
        assert adapter.count == 1
        wall[0] = 100.0
        await tick.tick()
        assert store.connection.execute('SELECT state FROM assurance_pending_work').fetchone()[0] == 'DONE'
        assert store.get_receipt('fixture-effect') is not None
        assert not store.connection.in_transaction
        descriptor = dict(store.connection.execute('SELECT version,name,checksum FROM orch_schema_migrations WHERE version=26').fetchone())
        store.close()
        return descriptor


def clean_seam():
    source = TypedRef(kind='tool_receipt', id='fixture-source', revision=1, content_hash='a'*64)
    candidates = []
    for oid, key, polarity in (('p','p',True),('n','p',False),('r','r',True)):
        observation = ObservationRecord(observation_id=oid,proposition_key=key,polarity=polarity,
            source_ref=source,observed_at_ms=1,recorded_at_ms=1,
            coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE,coverage_scope='mission',query_watermark_ms=1)
        candidates.append(AnchorCandidate(observation,scope_id='mission',source_group=oid))
    signatures = {key:PredicateSignature(VersionedRef(key,1,'a'*64)) for key in ('p','q','r')}
    selection = AnchorSelector(scope_id='mission',purpose=WitnessPurpose.ACCEPT,as_of_ms=2,signatures=signatures).select(candidates)
    tainted = JustificationSet('q',premises=(Atom('p'),),rule_version='fixture-v1')
    result = compute_grounded_support(SupportGraph((tainted,)),selection)
    assert str(result.supported.truth_for('q')) == 'TRUE' and not result.usable('q')
    alternative = JustificationSet('q',premises=(Atom('r'),),rule_version='fixture-v1')
    result = compute_grounded_support(SupportGraph((tainted,alternative)),selection)
    assert result.usable('q')


if __name__ == '__main__':
    descriptor = asyncio.run(tick_seam())
    clean_seam()
    result = {'scope':'narrow factory/tick/event-ref/clean-closure coding seam',
              'authority_and_consumers':'explicit fixtures; not deployment or acceptance',
              'schema_descriptor':descriptor,
              'checked':['activation seq + atomic seed','immutable original event full-body identity',
                         'source event barrier without wakeup recursion','rollback during timeout preserves highwater',
                         'no prepare while rollback','recovered original effect + DONE',
                         'conflicted upstream blocks derived TRUE; independent clean branch survives']}
    output = (EVIDENCE / (Path(__file__).stem + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json'))
    with output.open('x') as stream:
        stream.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
