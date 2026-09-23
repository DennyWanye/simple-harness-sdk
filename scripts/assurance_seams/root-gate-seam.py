"""Small coding seam: original backup/startup/Commit; explicit fixture ACL, no model."""
from seam_paths import SDK, EVIDENCE
import asyncio
import hashlib
import json
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.api.missions import MissionApi
from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.assurance.codec import AssuranceError, fingerprint
from agent_orchestrator.assurance.evidence import ReadItem
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.assurance.root_gate import STATE_FILE, CurrentReadPermission
from agent_orchestrator.contracts import Artifact, Attempt, Budget, Task
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.taskgraph_followups import TaskGraphFollowupPump
from agent_orchestrator.graph.notification_contracts import FollowupKind
from agent_orchestrator.assurance.root_gate import AssuranceRootGate
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.offline_backup import BackupSourceIdentity, backup_offline, restore_offline
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def refused(call, code=None):
    try:
        call()
    except (AssuranceError, FacadeError) as error:
        if code is not None:
            assert error.code == code, (error.code, code)
        return error.code
    raise AssertionError('expected refusal')


async def run():
    results = {}
    with TemporaryDirectory(prefix='assurance-root-seam-') as temp:
        base = Path(temp).resolve()
        cfg = OrchestratorConfig(evidence_root=base/'source')
        principal = Principal('fixture-current-user')
        provider = RoleScriptedProvider({})
        def install(orch):
            orch.commit.install_assurance_root(principal=principal, tenant_id='tenant', command_id='install')
        async with Orchestrator(cfg, provider, assurance_root_setup=install) as orch:
            assert not orch._assurance_management_only
            root_id = orch._assurance_root_gate.require_execution().root_incarnation_id
            profiles = orch._profiles
            mission, _ = orch.commit.create_mission(MissionSpec(goal='private title',
                success_criteria=('c',), tenant_id='tenant', idempotency_key='root-fixture'))
            task = Task('fixture-task',mission.id,(),(),'fixture','fixture',('file:answer.txt',),
                ('format_check',),(),Budget(),1,'READY',1)
            orch.store.insert_task(task, ordinal=0)
            attempt = Attempt('fixture-attempt', task.id, mission.id, 'worker','fixture-model',
                'fixture-prompt','fixture-context',Budget(),None,None,'VERIFYING',None,
                orch.store.now,1,1,'fixture-create','fixture-input')
            orch.store.insert_attempt(attempt)
            cas = orch.assembled.workspaces.artifact_store
            content = b'private restored content\n'
            digest = cas.put_bytes(content)
            artifact = Artifact('fixture-artifact',mission.id,task.id,attempt.id,'text','answer.txt',
                1,digest,len(content),'fixture',storage_uri=str(cas.path_for(digest)))
            orch.store.upsert_artifact(artifact)
            assert provider.calls == 0
            calls = []
            saved_state = (cfg.evidence_root/STATE_FILE).read_bytes()
            (cfg.evidence_root/STATE_FILE).unlink()
            assert AssuranceRootGate.required(orch.store,cfg.evidence_root)
            # Execute the real executor's worker boundary, with a harmless fixture connector.
            try:
                await asyncio.to_thread(orch._actions._external_call,lambda: calls.append('external'))
            except AssuranceError:
                pass
            else:
                raise AssertionError('worker boundary failed to refuse')
            assert calls == []
            (cfg.evidence_root/STATE_FILE).write_bytes(saved_state)
            (cfg.evidence_root/STATE_FILE).chmod(0o600)
            # Inject only a claim result to target the post-claim race, not notification persistence.
            async def handler(*args):
                calls.append('consumer')
                raise AssertionError('consumer must not run')
            def claim(*args,**kwargs):
                (cfg.evidence_root/STATE_FILE).unlink()
                return SimpleNamespace(message=SimpleNamespace(kind=FollowupKind.REEVALUATE),
                    command_key='fixture-command',message_id='fixture-message')
            notifications = SimpleNamespace(store=orch.store,claim_followup=claim,
                retry_followup=lambda *args,**kwargs:'WAITING')
            pump = TaskGraphFollowupPump(notifications,owner='fixture',reevaluate=handler,
                converge=handler,request_composition=handler)
            assert (await pump.pump_once()).state == 'WAITING' and calls == []
            (cfg.evidence_root/STATE_FILE).write_bytes(saved_state)
            (cfg.evidence_root/STATE_FILE).chmod(0o600)
            results['worker_connector_and_post_claim_consumer_gate_fixture_callbacks'] = True
        # No execution leases were acquired; backup performs its real offline checks.
        backup = backup_offline(cfg, profiles, WorkspaceManager(cfg.workspaces_root),
            instance_lock_path=cfg.evidence_root/'.instance.lock', destination=base/'bundle',
            source_identity=BackupSourceIdentity('a'*40,'b'*64))
        manifest = json.loads((base/'bundle'/'manifest.json').read_text())
        assert STATE_FILE not in manifest['files'] and not (base/'bundle'/STATE_FILE).exists()
        restored = restore_offline(base/'bundle', destination=base/'restored',
            expected_manifest_sha256=backup['manifest_sha256'])
        new_cfg = OrchestratorConfig(evidence_root=base/'restored')
        assert restored['root_incarnation_id'] != root_id
        assert not (new_cfg.evidence_root/STATE_FILE).exists()
        results['managed_backup_restore_new_identity_no_live_grant'] = True
        clock = [2_000_000_000.0]
        allowed = [True]
        policy = ['policy-1']
        def authority(p, tenant, mid, ref, purpose):
            assert p == principal and tenant == 'tenant' and mid == mission.id
            assert purpose == 'DISCLOSE'
            if not allowed[0]:
                raise AssuranceError('FIXTURE_CURRENT_AUTHORITY_REFUSED')
            return CurrentReadPermission(ReadItem('ACCESS','fixture-current-acl',fingerprint('acl-1')),
                ReadItem('POLICY','fixture-current-policy',fingerprint(policy[0])),
                int(clock[0]*1000)+30000)
        def setup(orch):
            orch.store._clock = lambda: clock[0]
            orch.commit._assurance_read_authority = authority
        async with Orchestrator(new_cfg, provider, assurance_root_setup=setup,
                                startup_assembly=lambda _: (_ for _ in ()).throw(AssertionError('runtime started'))) as orch:
            assert orch._assurance_management_only and orch._assembled is None
            api = MissionControlV1(orch,tenant_id='tenant',principal=principal)
            assert api.assurance_root_diagnostic()['state'] == 'QUARANTINED'
            refused(api.missions)
            refused(lambda: api.snapshot(mission.id))
            refused(lambda: MissionApi(orch.commit).get(mission.id))
            refused(lambda: api.artifact_read(artifact.id))
            refused(orch._require_assurance_execution_root)
            identity = orch._assurance_root_gate.restored_identity()
            target = {'mission_id':mission.id, 'ref':AssuranceRef('artifact',Pin(artifact.id,1,digest)).to_json(),
                      'purpose':'DISCLOSE'}
            command = {'command_id':'grant-1','root_incarnation_id':identity.root_incarnation_id,
                'restore_manifest_hash':identity.restore_manifest_hash,'targets':[target], 'ttl_ms':20000}
            # Simulated exact crash window AFTER original receipt commit, BEFORE state publish.
            with patch('agent_orchestrator.orchestrator.assurance_root_commits._publish',
                       side_effect=OSError('fixture crash after DB commit')):
                try:
                    api.reauthorize_restored_read(command)
                except OSError:
                    pass
                else:
                    raise AssertionError('fault did not fire')
            assert api.assurance_root_diagnostic()['state'] == 'QUARANTINED'
            refused(lambda: api.artifact_read(artifact.id))
            grant = api.reauthorize_restored_read(command)
            assert api.reauthorize_restored_read(command) == grant
            assert api.artifact_read(artifact.id)['content'] == content.decode()
            assert api.assurance_root_diagnostic()['state'] == 'READ_ONLY_REAUTHORIZED'
            refused(orch._require_assurance_execution_root,'RESTORED_EXECUTION_REQUIRES_RECOVERY')
            refused(api.missions)
            results['original_startup_quarantine_and_receipt_then_file_repair'] = True
            results['exact_artifact_read_without_execution_resume'] = True
            other = MissionControlV1(orch,tenant_id='tenant',principal=Principal('other-user'))
            old_authority = orch.commit._assurance_read_authority
            orch.commit._assurance_read_authority = lambda *args: authority(principal,*args[1:])
            refused(lambda: other.artifact_read(artifact.id),'ROOT_READ_NOT_AUTHORIZED')
            cross = MissionControlV1(orch,tenant_id='other-tenant',principal=principal)
            refused(lambda: cross.artifact_read(artifact.id),'not_found')
            orch.commit._assurance_read_authority = old_authority
            # Partial restore fails even with a previously issued protected grant.
            execution_db = next(p for p in new_cfg.evidence_root.glob('*.db') if p.name != 'orchestrator.db')
            moved = execution_db.with_suffix('.missing')
            execution_db.rename(moved)
            refused(lambda: api.artifact_read(artifact.id),'RESTORE_DATABASE_INCOMPLETE')
            moved.rename(execution_db)
            results['fixed_caller_tenant_and_partial_database_refuse'] = True
            allowed[0] = False
            refused(lambda: api.artifact_read(artifact.id),'FIXTURE_CURRENT_AUTHORITY_REFUSED')
            allowed[0] = True
            policy[0] = 'changed-policy'
            refused(lambda: api.artifact_read(artifact.id),'ROOT_READ_NOT_AUTHORIZED')
            policy[0] = 'policy-1'
            clock[0] += 1
            assert api.artifact_read(artifact.id)['content'] == content.decode()
            clock[0] -= .5
            refused(lambda: api.artifact_read(artifact.id),'TIME_DISCONTINUITY')
            clock[0] += 30
            refused(lambda: api.artifact_read(artifact.id),'ROOT_READ_NOT_AUTHORIZED')
            assert api.assurance_root_diagnostic()['state'] == 'QUARANTINED'
            results['current_acl_policy_expiry_and_persisted_clock_rollback_refuse'] = True
            assert provider.calls == 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    output = (EVIDENCE / ('root-gate-seam-'+stamp+'.json'))
    with output.open('x') as stream:
        sdk = SDK
        files = ['assurance/root_gate.py','orchestrator/assurance_root_commits.py',
            'orchestrator/event_handler.py','api/facade.py','runtime/actions.py',
            'orchestrator/taskgraph_followups.py','storage/offline_backup.py']
        hashes = {name:hashlib.sha256((sdk/'src/agent_orchestrator'/name).read_bytes()).hexdigest()
                  for name in files}
        json.dump({'scope':'coding seam only; fixture current authority/claim/connector; no Host/UI/model acceptance',
            'results':results,'provider_calls':provider.calls,'source_sha256':hashes},stream,indent=2)
    print(output)


asyncio.run(run())
