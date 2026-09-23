"""code_test through the actual sandbox executor -> execution receipt -> CheckBinding.

BW05 executor branch on the fixture Mission: the verifier's real ``code_test``
(pytest in a child process through ``ProcessOnlyExecutor``), the trusted
recorder importing the executor's own receipts (never re-running), the original
Commit import to an immutable CheckBinding, and the human check-policy approval
that now resolves ``required_check_ids=("code_test",)``. Counter-cases: failing
test, workspace mutated by the tests, timeout, a layer without executor facts,
exit 0 without reported node ids. Fixture root/ACL; no real model, no Host/UI.
"""
from _assured_fixture import (EVIDENCE, SDK, TENANT, build_world, count, refused, requirements_ref,  # noqa: F401
                              source_sha256)
import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import jsonschema
from referencing import Registry, Resource
import test_completion_spec_approval as approval
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.assurance.check_specs import CheckSpec
from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import decode
from agent_orchestrator.assurance.refs import AssuranceRef
from agent_orchestrator.contracts.resolution import (AllExpr, CriterionExpr, EvaluationKind, RequiredEvidencePolicy,
                                                     RequirementsRevision)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_local_checks import AssuranceLocalChecks
from agent_orchestrator.runtime.sandbox import ProcessOnlyExecutor
from agent_orchestrator.storage.assurance_reads import AssuranceReader
from agent_orchestrator.verification.assurance_local import freeze_verifier_inputs
from agent_orchestrator.verification.deterministic_checks import LayerResult, code_test

SCHEMAS = SDK / 'src/agent_orchestrator/assurance/schemas'
registry = Registry()
for name in ('common.schema.json', 'check-spec-v1.schema.json', 'local-check-receipt-v1.schema.json', 'check-binding-v2.schema.json'):
    doc = json.loads((SCHEMAS / name).read_text())
    registry = registry.with_resource(doc['$id'], Resource.from_contents(doc))


def validate(name, body):
    jsonschema.Draft202012Validator(json.loads((SCHEMAS / name).read_text()), registry=registry).validate(body)


def deterministic_requirements(mission, spec):
    """criterion-report is DETERMINISTIC and requires the code_test layer."""
    report = dataclasses.replace(approval._criterion('criterion-report'), evaluation_kind=EvaluationKind.DETERMINISTIC,
                                 required_evidence_policy=RequiredEvidencePolicy(required_check_ids=('code_test',)))
    return RequirementsRevision(revision_id='completion-requirements-1', mission_id=mission.id, revision=1,
        criteria=(report, approval._criterion('criterion-delivered')),
        success_expression=AllExpr((CriterionExpr('criterion-report'), CriterionExpr('criterion-delivered'))),
        authority_subject='authenticated-user-confirmation')


PASSING = "def test_report_ok():\n    assert True\n\ndef test_second():\n    assert 1 + 1 == 2\n"
FAILING = "def test_report_bad():\n    assert False, 'fixture failure'\n"
MUTATING = ("from pathlib import Path\n\ndef test_touches_input():\n"
            "    Path('report.json').write_text('changed by the test')\n    assert True\n")
SLOW = "import time\n\ndef test_slow():\n    time.sleep(5)\n"


def make_copy(root, name, test_source, artifact_bytes, attempt_id, cas):
    folder = root / name
    (folder / 'tests').mkdir(parents=True)
    (folder / 'report.json').write_bytes(artifact_bytes)
    (folder / 'tests' / 'test_fixture.py').write_text(test_source)
    return Workspace(folder, attempt_id, False, cas)


def binding_of(store, ref):
    row = store.connection.execute('SELECT binding_json FROM assurance_check_bindings WHERE execution_ref_hash=?', (ref.key,)).fetchone()
    return None if row is None else decode(row[0])


async def main():
    report = {'scope': 'actual code_test through ProcessOnlyExecutor (real pytest child process) -> trusted recorder '
                       'imports the executor receipts (no re-run) -> AssuranceExecutionImported event/receipt -> '
                       'original Commit import -> CheckBinding(execution_receipt); CHECKED policy approval for '
                       'required_check_ids=("code_test",); counters; fixture Mission/root/ACL; not the full '
                       'VerifierRouter pass (rule/critic layers), not a real model, not Host/UI'}
    with TemporaryDirectory(prefix='assurance-executor-check-') as temp:
        root = Path(temp).resolve()
        world, task, stored, artifact, scope_ref = build_world(root, requirements_fn=deterministic_requirements, approve_policy=False)
        store, commit, mission = world.store, world.service, world.mission
        cas = ArtifactStore(root / 'scoped-cas')
        adapter = AssuranceLocalChecks(commit, tenant_id=TENANT, cas=cas, require_current_root=commit._assurance_root_gate.require_execution)
        reader = AssuranceReader(store, tenant_id=TENANT, mission_id=mission.id)
        registered = adapter._registry(mission.id)
        assert set(registered) == {'format_check', 'rule_check', 'code_test'}, set(registered)
        spec_ref = registered['code_test'].binding.spec_ref
        spec_body = decode(reader.read_exact_metadata(spec_ref).body_json)['payload']
        validate('check-spec-v1.schema.json', spec_body)
        assert CheckSpec.from_json(spec_body).execution_kind == 'EXECUTOR'
        # 1. Human approval: a DETERMINISTIC criterion requiring code_test maps to the
        #    registered executor CheckSpec; SEMANTIC for it is refused, as before.
        req = world.semantics.get_requirements_revision(mission.id, 1)
        approve = dict(tenant_id=TENANT, mission_id=mission.id, command_id='fixture-code-test-policy',
                       principal=Principal('fixture-authenticated-user'), requirements_ref=requirements_ref(req), completion_scope=scope_ref)
        report['policy_semantic_refused'] = refused(lambda: commit.approve_assurance_check_policy(
            **approve, candidate_mapping=(CriterionPolicy('criterion-report', 'SEMANTIC', ()),)), {'CHECK_POLICY_UNRESOLVED'})
        policy_ref = commit.approve_assurance_check_policy(**approve, candidate_mapping=(CriterionPolicy('criterion-report', 'CHECKED', ((spec_ref,),)),))
        assert count(store, 'SELECT COUNT(*) FROM assurance_criterion_policies') == 1
        report['policy_checked'] = policy_ref.pin.id
        # 2. The verifier's actual run: pytest in a child process through the executor port.
        body = cas.path_for(artifact.content_hash).read_bytes()
        bound_artifact = dataclasses.replace(artifact, path='report.json')
        exec_task = dataclasses.replace(task, verification_policy=('format_check', 'rule_check', 'code_test', 'critic_review'),
                                        success_criteria=('pytest:tests',))
        executor = ProcessOnlyExecutor()

        def inputs_for(copy):
            return freeze_verifier_inputs(mission=mission, task=exec_task, envelope=stored.envelope, artifacts=(bound_artifact,),
                verification_copy=copy, client_result_id=None, tampered=(), knowledge=None, require_synthesis_knowledge=False,
                action_problems=None, local_code_execution=True, domain=None, assessment_binding=None)

        async def run(name, source, *, timeout=60.0):
            copy = make_copy(root, name, source, body, stored.envelope.attempt_id, cas)
            recorder = adapter.prepare(inputs_for(copy))
            actual = await code_test(exec_task, verification_copy=copy, timeout=timeout, executor=executor,
                                     result_id=stored.envelope.id, artifacts=(bound_artifact,))
            imported = recorder.record_executor('code_test', actual)
            ref = AssuranceRef.from_json(imported.detail['assurance_executor_check_ref'])
            assert ref.kind == 'execution_receipt'
            event = decode(reader.read_exact_metadata(ref).body_json)
            assert event['type'] == 'AssuranceExecutionImported'
            validate('local-check-receipt-v1.schema.json', event['payload']['executor_check'])
            bound = binding_of(store, ref)
            assert bound is not None, 'binding must be written in the same original import'
            validate('check-binding-v2.schema.json', bound)
            evidence = decode(reader.read_exact_metadata(AssuranceRef.from_json(bound['evidence_refs'][0])).body_json)
            document = json.loads(cas.path_for(evidence['content_hash']).read_bytes())
            return actual, imported, ref, event, bound, document

        actual, imported, ref, event, bound, document = await run('passing', PASSING)
        assert actual.status == 'PASS' and imported.status == 'PASS'
        assert bound['execution_state'] == 'SUCCEEDED' and bound['verdict'] == 'PASS', bound
        assert bound['execution_ref']['kind'] == 'execution_receipt' and bound['check_spec_ref'] == spec_ref.to_json()
        run0 = document['runs'][0]
        assert run0['receipt']['execution_id'] and run0['receipt']['kind'] == 'process_only' and run0['receipt']['exit_code'] == 0
        assert sorted(run0['nodeids']['PASSED']) == ['tests/test_fixture.py::test_report_ok', 'tests/test_fixture.py::test_second'], run0['nodeids']
        assert document['observation_scope']['result_id'] == stored.envelope.id
        assert document['observation_scope']['artifact_hashes'] == {'report.json': artifact.content_hash}
        assert event['payload']['execution']['execution_state'] == 'SUCCEEDED'
        report['passing'] = {'binding_verdict': bound['verdict'], 'state': bound['execution_state'],
                             'nodeids': run0['nodeids']['PASSED'], 'execution_id': run0['receipt']['execution_id'],
                             'environment_digest': run0['receipt']['environment_digest'], 'bound_to_result': True}
        # 3. Exact replay of the original import: same binding, no second row.
        with store.transaction():
            first = commit.import_assurance_check_locked(mission_id=mission.id, execution_ref=ref, completion_scope=scope_ref)
            again = commit.import_assurance_check_locked(mission_id=mission.id, execution_ref=ref, completion_scope=scope_ref)
        assert first == again and count(store, 'SELECT COUNT(*) FROM assurance_check_bindings') == 1
        assert count(store, "SELECT COUNT(*) FROM commit_receipts WHERE kind='AssuranceExecutorCheckImported'") == 1
        report['replay_idempotent'] = True
        # 4. Counters, each a real executor run.
        actual, imported, ref, event, bound, document = await run('failing', FAILING)
        assert actual.status == 'FAIL' and bound['execution_state'] == 'SUCCEEDED' and bound['verdict'] == 'FAIL'
        assert document['runs'][0]['nodeids']['FAILED'] == ['tests/test_fixture.py::test_report_bad']
        report['failing'] = {'binding_verdict': bound['verdict'], 'failed_nodeids': document['runs'][0]['nodeids']['FAILED']}
        actual, imported, ref, event, bound, document = await run('mutating', MUTATING)
        assert actual.status == 'PASS' and 'observation_scope' not in actual.detail
        assert bound['execution_state'] == 'SUCCEEDED' and bound['verdict'] == 'UNKNOWN', bound
        report['workspace_mutated'] = {'layer_status': actual.status, 'binding_verdict': bound['verdict'], 'reason': document['reason']}
        actual, imported, ref, event, bound, document = await run('slow', SLOW, timeout=1.0)
        assert actual.status == 'FAIL' and document['runs'][0]['timed_out']
        assert bound['execution_state'] == 'CANCELLED' and bound['verdict'] == 'UNKNOWN', bound
        report['timeout'] = {'state': bound['execution_state'], 'binding_verdict': bound['verdict']}
        # 5. Facts the executor never produced cannot be imported as a run: a layer
        #    without receipts is ERROR/UNKNOWN; exit 0 without node ids is UNKNOWN.
        copy = make_copy(root, 'synthetic', PASSING, body, stored.envelope.attempt_id, cas)
        recorder = adapter.prepare(inputs_for(copy))
        no_receipt = LayerResult('code_test', 'PASS', 'claimed', {'runs': [{'target': 'tests', 'returncode': 0, 'stdout': 'PASSED tests/test_fixture.py::test_report_ok', 'timed_out': False, 'command': [], 'passed': True}]})
        imported = recorder.record_executor('code_test', no_receipt)
        bound = binding_of(store, AssuranceRef.from_json(imported.detail['assurance_executor_check_ref']))
        assert bound['execution_state'] == 'ERROR' and bound['verdict'] == 'UNKNOWN'
        report['no_executor_receipt'] = {'state': bound['execution_state'], 'binding_verdict': bound['verdict']}
        silent = LayerResult('code_test', 'PASS', 'claimed', {'runs': [{'target': 'tests', 'returncode': 0, 'stdout': '2 passed in 0.01s', 'timed_out': False, 'command': [],
                             'passed': True, 'receipt': {'execution_id': 'synthetic-clean-run', 'kind': 'process_only', 'isolated': False,
                             'environment_digest': 'e' * 64, 'effective_limits': {}, 'exit_code': 0, 'output': '', 'truncated': False, 'timed_out': False,
                             'limit_exceeded': None, 'tree_killed': True, 'residual_pids': [], 'status': 'ok'}}],
                             'observation_scope': {'result_id': stored.envelope.id, 'artifact_hashes': {}}})
        imported = recorder.record_executor('code_test', silent)
        bound = binding_of(store, AssuranceRef.from_json(imported.detail['assurance_executor_check_ref']))
        assert bound['execution_state'] == 'SUCCEEDED' and bound['verdict'] == 'UNKNOWN', bound
        report['exit0_without_nodeids'] = {'state': bound['execution_state'], 'binding_verdict': bound['verdict'], 'synthetic_layer': True}
        report['bindings_total'] = count(store, 'SELECT COUNT(*) FROM assurance_check_bindings')
        report['events'] = {'AssuranceExecutionImported': store.count_events(mission.id, 'AssuranceExecutionImported'),
                            'AssuranceCheckBound': store.count_events(mission.id, 'AssuranceCheckBound')}
        assert report['events']['AssuranceExecutionImported'] == 6 == report['bindings_total']
        store.close()
    report['status'] = 'PASS'
    report['source_sha256'] = source_sha256(['assurance/check_specs.py', 'assurance/executor_checks.py', 'assurance/local_checks.py',
        'verification/assurance_local.py', 'verification/verifier_router.py', 'verification/deterministic_checks.py',
        'runtime/tool_gateway.py', 'orchestrator/assurance_local_checks.py', 'orchestrator/assurance_check_import.py',
        'orchestrator/assurance_check_policy.py'])
    out = EVIDENCE / ('executor-check-seam-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': 'PASS', 'evidence': str(out), 'passing': report['passing']['binding_verdict'], 'failing': report['failing']['binding_verdict'],
                      'workspace_mutated': report['workspace_mutated']['binding_verdict'], 'timeout': report['timeout'],
                      'no_executor_receipt': report['no_executor_receipt'], 'exit0_without_nodeids': report['exit0_without_nodeids']['binding_verdict'],
                      'policy': report['policy_semantic_refused']}, ensure_ascii=False))


asyncio.run(main())
