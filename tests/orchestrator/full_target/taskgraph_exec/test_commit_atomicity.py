# SPDX-License-Identifier: Apache-2.0
"""Abort actual SQLite writes after the original planning authority checks."""
import asyncio
import sqlite3

import pytest

from production_fixture import enabled_world
from agent_orchestrator.storage.store import StoreConflict, StoreError
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.artifacts.store import read_nofollow


@pytest.mark.parametrize('point,table,predicate', (
    ('applied', 'planning_admission_checks', "NEW.phase='APPLIED'"),
    ('record', 'taskgraph_revision_records', '1'),
    ('followup', 'taskgraph_followups', '1'),
))
def test_commit_write_failure_leaves_no_plan_tasks_budget_or_graph_receipt(tmp_path, point, table, predicate):
    async def case():
        async with enabled_world(tmp_path, key='tg-atomic-' + point) as world:
            store, mission = world.loop.store, world.mission.id
            account_ids = {r[0] for r in store.connection.execute(
                'SELECT account_id FROM budget_accounts WHERE mission_id=?', (mission,))}
            existing_intents = {r[0] for r in store.connection.execute(
                'SELECT intent_id FROM dispatch_intents WHERE mission_id=?', (mission,))}
            # A trigger is fault injection into the actual transaction, not a
            # substitute compiler/Store/Commit return value. Remove only this
            # test-owned trigger after the attempt; source constraints remain.
            store.connection.execute(
                f"CREATE TRIGGER tg_test_abort BEFORE INSERT ON {table} WHEN {predicate} "
                "BEGIN SELECT RAISE(ABORT,'TG_TEST_ATOMIC_WRITE'); END")
            try:
                with pytest.raises((sqlite3.DatabaseError, StoreError)):
                    await world.loop._dispatch(world.intent)
            finally:
                store.connection.execute('DROP TRIGGER tg_test_abort')
            assert store.list_tasks(mission) == []
            for name in ('plan_revisions', 'taskgraph_revision_records', 'taskgraph_member_pins',
                         'taskgraph_method_pins', 'taskgraph_followups', 'attempts'):
                assert store.connection.execute(
                    f'SELECT COUNT(*) FROM {name} WHERE mission_id=?', (mission,)).fetchone()[0] == 0
            assert {r[0] for r in store.connection.execute(
                'SELECT account_id FROM budget_accounts WHERE mission_id=?', (mission,))} == account_ids
            assert {r[0] for r in store.connection.execute(
                'SELECT intent_id FROM dispatch_intents WHERE mission_id=?', (mission,))} == existing_intents
            assert store.connection.execute(
                "SELECT COUNT(*) FROM planning_admission_checks WHERE request_id=? AND phase='APPLIED'",
                (world.intent.intent_id,)).fetchone()[0] == 0
            assert not any(event.type in {'PlanRevisionCommitted', 'TaskGraphRevisionRecorded'}
                           for event in store.iter_events(mission))
            assert world.provider.calls == 0
    asyncio.run(case())


def test_committed_reply_replay_after_revoke_is_readonly_and_changed_bytes_conflict(tmp_path):
    async def case():
        async with enabled_world(tmp_path, key='tg-committed-replay') as world:
            await world.commit_seed()
            store, mission = world.loop.store, world.mission.id
            decisions = PlanningDecisionStore(store)
            request = decisions.get_planning_request_for_intent(world.intent.intent_id)
            assert request is not None
            decision = decisions.get_planning_decision_by_attempt(request.request_id,
                world.loop._planning_decision_attempt_ordinal(world.intent))
            assert decision is not None and decision['status'] == 'COMMITTED'
            raw = read_nofollow(world.loop.assembled.workspaces.artifact_store.path_for(
                decision['raw_artifact_ref'])).decode('utf-8')
            world.authorization.revoke(world.grant.grant_id, expected_revision=world.grant.revision,
                command_id='revoke-after-commit', reason='Verify historical receipt replay')
            before = (store.connection.total_changes, world.provider.calls)
            current = store.get_mission(mission)
            await world.loop._collect_plan_decision(world.intent, None, current, raw, world.dispatch)
            assert (store.connection.total_changes, world.provider.calls) == before
            with pytest.raises(StoreConflict):
                await world.loop._collect_plan_decision(world.intent, None, current, raw + '\n', world.dispatch)
            assert (store.connection.total_changes, world.provider.calls) == before
    asyncio.run(case())
