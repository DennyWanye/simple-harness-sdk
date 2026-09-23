# SPDX-License-Identifier: Apache-2.0
"""Process-exit fault driver; only run with a fresh ignored evidence directory."""
import asyncio
import json
import os
from pathlib import Path
import sys

from production_fixture import enabled_world
from agent_orchestrator.testing.fixtures import envelope_step


async def main(root: Path, mode: str):
    root.mkdir(parents=True, exist_ok=True)
    async with enabled_world(root, key='tg-process-' + mode,
                             worker_steps=(('workspace_write_file', {'path': 'facts.md', 'content': 'Repository facts for the interrupted attempt.'}),
                                envelope_step(summary='Recorded repository facts.', artifacts=['facts.md'], claims=[],
                                              override=lambda value: {**value, 'outputs': {'facts': 'facts.md'}}))) as world:
        before_worker = None
        def exit_with_original_reply(code):
            row = world.loop.store.connection.execute(
                'SELECT raw_artifact_ref,raw_output_hash FROM planning_decisions WHERE request_id=?',
                (world.intent.intent_id,)).fetchone()
            assert row is not None
            worker = world.loop.store.connection.execute(
                "SELECT intent_id,subject_id FROM dispatch_intents WHERE kind='attempt' ORDER BY created_at LIMIT 1").fetchone()
            (root / 'recovery-source.json').write_text(json.dumps({
                'mission_id': world.mission.id, 'intent_id': world.intent.intent_id,
                'issuer_id': world.loop._owner, 'raw_artifact_ref': row[0], 'raw_output_hash': row[1],
                'repository': str(root / 'repo'), 'fault_mode': mode,
                'before_worker': before_worker,
                'worker_intent_id': None if worker is None else worker[0],
                'worker_attempt_id': None if worker is None else worker[1],
            }) + '\n')
            os._exit(code)
        if mode == 'inside_commit':
            world.loop.store.connection.create_function('tg_exit', 0, lambda: exit_with_original_reply(81))
            world.loop.store.connection.execute(
                'CREATE TEMP TRIGGER tg_exit_before_record BEFORE INSERT ON taskgraph_revision_records '
                'BEGIN SELECT tg_exit(); END')
        await world.commit_seed()
        if mode == 'after_commit':
            exit_with_original_reply(82)
        before_worker = {
            'account_ids': [r[0] for r in world.loop.store.connection.execute(
                'SELECT account_id FROM budget_accounts ORDER BY account_id')],
            'reservation_ids': [r[0] for r in world.loop.store.connection.execute(
                'SELECT reservation_id FROM budget_reservations ORDER BY reservation_id')],
        }
        if mode == 'after_reserve':
            world.loop.store.connection.create_function('tg_exit', 0, lambda: exit_with_original_reply(83))
            world.loop.store.connection.execute(
                "CREATE TEMP TRIGGER tg_exit_before_attempt_intent BEFORE INSERT ON dispatch_intents "
                "WHEN NEW.kind='attempt' BEGIN SELECT tg_exit(); END")
        elif mode == 'after_executor':
            original_fault = world.loop.store.fault
            def fault(point, kind=None):
                if point == 'after_turn_committed' and kind == 'attempt':
                    exit_with_original_reply(84)
                return original_fault(point, kind)
            world.loop.store.fault = fault
        else:
            raise AssertionError('unexpected fault mode')
        async with asyncio.timeout(20):
            while True:
                await world.loop._cycle()
                await asyncio.sleep(.01)
        raise AssertionError('process exit point was not reached')


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
