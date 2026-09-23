# SPDX-License-Identifier: Apache-2.0
"""C03 plan transaction windows; reserve and physical-turn windows remain separate."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import sqlite3

import pytest

from production_fixture import _config
from agent_orchestrator.artifacts.store import read_nofollow
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import deployed_layers
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.taskgraph_assembly import TaskGraphDeploymentPorts
from agent_orchestrator.orchestrator.taskgraph_deployment import InstalledHtnWiringAcceptance
from agent_orchestrator.planning.htn.observers.code import code_observers
from agent_orchestrator.planning.htn.world import build_planning_world
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import Store
from agent_orchestrator.storage.taskgraph_store import TaskGraphStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


@pytest.mark.parametrize('mode,exit_code,committed', (
    ('inside_commit', 81, 0), ('after_commit', 82, 1),
))
def test_process_exit_reuses_original_reply_and_commit_identity(tmp_path, mode, exit_code, committed):
    child = subprocess.run([sys.executable, str(Path(__file__).with_name('crash_seed.py')),
        str(tmp_path), mode], capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / 'child.log').write_text(child.stdout + child.stderr)
    assert child.returncode == exit_code, (child.returncode, child.stdout, child.stderr)
    source = json.loads((tmp_path / 'recovery-source.json').read_text())
    store = Store.open(_config(tmp_path).orchestrator_db)
    try:
        for table in ('plan_revisions', 'taskgraph_revision_records'):
            assert store.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == committed
        assert store.connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
    finally:
        store.close()

    async def recover():
        provider = RoleScriptedProvider({})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission = loop.store.get_mission(source['mission_id'])
            intent = loop.store.get_intent(source['intent_id'])
            assert mission is not None and intent is not None
            planning = build_planning_world(mission.id, domains=('code',), semantics=HtnStore(loop.store),
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(source['repository'], allow_test_execution=True))
            dispatch = loop.install_hierarchical(planning=planning)
            loop.install_taskgraph(TaskGraphDeploymentPorts(tenant_id=mission.tenant_id,
                principal=Principal(source['issuer_id']), deployment_acceptance=InstalledHtnWiringAcceptance(),
                graph_budget=DEFAULT_PROJECTION_BUDGET))
            raw = read_nofollow(loop.assembled.workspaces.artifact_store.path_for(source['raw_artifact_ref']))
            assert hashlib.sha256(raw).hexdigest() == source['raw_output_hash'] == source['raw_artifact_ref']
            before = loop.store.connection.total_changes
            await loop._collect_plan_decision(intent, None, mission, raw.decode('utf-8'), dispatch)
            if committed:
                assert loop.store.connection.total_changes == before
            record = TaskGraphStore(loop.store).read_revision(mission.id, 1).record
            assert record.command_id == 'plan:' + source['intent_id']
            assert loop.store.connection.execute('SELECT COUNT(*) FROM plan_revisions').fetchone()[0] == 1
            assert loop.store.connection.execute('SELECT COUNT(*) FROM taskgraph_revision_records').fetchone()[0] == 1
            assert loop.store.connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
            assert provider.calls == 0
    asyncio.run(recover())


@pytest.mark.parametrize('mode,exit_code,attempt_count', (
    ('after_reserve', 83, 0), ('after_executor', 84, 1),
))
def test_process_exit_at_attempt_boundary_preserves_original_accounting(tmp_path, mode, exit_code, attempt_count):
    child = subprocess.run([sys.executable, str(Path(__file__).with_name('crash_seed.py')),
        str(tmp_path), mode], capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / 'child.log').write_text(child.stdout + child.stderr)
    assert child.returncode == exit_code, (child.returncode, child.stdout, child.stderr)
    source = json.loads((tmp_path / 'recovery-source.json').read_text())
    config = _config(tmp_path)

    def physical_calls():
        with sqlite3.connect(config.execution_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
            return [tuple(row) for row in db.execute(
                'SELECT invocation_id,state,handoff_attempt FROM provider_invocations ORDER BY invocation_id')]

    before_calls = physical_calls()
    store = Store.open(config.orchestrator_db)
    try:
        for table in ('attempts', 'taskgraph_attempt_inputs'):
            assert store.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == attempt_count
        assert store.connection.execute(
            "SELECT COUNT(*) FROM dispatch_intents WHERE kind='attempt'").fetchone()[0] == attempt_count
        assert store.connection.execute('SELECT COUNT(*) FROM taskgraph_revision_records').fetchone()[0] == 1
        if mode == 'after_reserve':
            assert before_calls == []
            assert [r[0] for r in store.connection.execute(
                'SELECT account_id FROM budget_accounts ORDER BY account_id')] == source['before_worker']['account_ids']
            assert [r[0] for r in store.connection.execute(
                'SELECT reservation_id FROM budget_reservations ORDER BY reservation_id')] == source['before_worker']['reservation_ids']
        else:
            assert len(before_calls) == 2  # One tool request and the final result envelope.
            assert all(state == 'succeeded' and handoffs == 1 for _, state, handoffs in before_calls)
    finally:
        store.close()
    if mode == 'after_reserve':
        return

    async def recover():
        provider = RoleScriptedProvider({})
        def assemble_startup(loop):
            mission = loop.store.get_mission(source['mission_id'])
            planning = build_planning_world(mission.id, domains=('code',), semantics=HtnStore(loop.store),
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(source['repository'], allow_test_execution=True))
            loop.install_hierarchical(planning=planning)
            loop.install_taskgraph(TaskGraphDeploymentPorts(tenant_id=mission.tenant_id,
                principal=Principal(source['issuer_id']), deployment_acceptance=InstalledHtnWiringAcceptance(),
                graph_budget=DEFAULT_PROJECTION_BUDGET))
        async with Orchestrator(config, provider, startup_assembly=assemble_startup) as loop:
            intent = loop.store.get_intent(source['worker_intent_id'])
            assert intent is not None and intent.state == 'SUBMITTED'
            assert await loop._collect(intent)
            assert loop.store.get_intent(intent.intent_id).state == 'SETTLED'
            # The original recovery pass imports durable physical usage after collection.
            await loop.recover()
            assert loop.commit.ledger.reservation(source['worker_attempt_id'])['state'] == 'SETTLED'
            assert provider.calls == 0 and physical_calls() == before_calls
            assert loop.store.connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
            assert loop.store.connection.execute('SELECT COUNT(*) FROM taskgraph_revision_records').fetchone()[0] == 1
    asyncio.run(recover())
