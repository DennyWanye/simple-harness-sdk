"""Real old Runtime expiry -> explicit exact recovery, without restamping fixtures."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from simple_harness import RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import DecisionState, UnitOfWorkConflict
from simple_harness.tools.authorization import AuthorizationDecision
from .test_react_sqlite_runtime import (
    AuthorizationScenario, PhysicalToolCounter, authorization_runtime, start_authorization_wait,
)


@pytest.fixture(scope='module')
def legacy_database(tmp_path_factory):
    target = os.environ.get('H077_LEGACY_075_TARGET')
    if not target:
        pytest.fail('H077_LEGACY_075_TARGET must name preserved installed H075, no synthetic fallback')
    path = tmp_path_factory.mktemp('genuine-old-expiry') / 'legacy.db'
    subprocess.run([sys.executable, '-I', str(Path(__file__).with_name('legacy_expiry_fixture.py')),
                    target, str(path)], check=True, timeout=25)
    return path


@pytest.fixture
def old_uow(legacy_database, tmp_path):
    path = tmp_path / 'copy.db'
    with sqlite3.connect(legacy_database.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(path) as target:
            source.backup(target)
    db = Database.open(path, wal=True)
    try:
        yield SqliteExecutionUnitOfWork(db)
    finally:
        db.close()


def _old_rows(db):
    return {table: [tuple(row) for row in db.connection.execute('SELECT * FROM ' + table)]
            for table in ('runs', 'decisions', 'run_start_snapshots', 'execution_effects')}


def _read(uow):
    return uow.read_expired_authorization_terminal_recovery(RunId('run-fault'))


@pytest.mark.parametrize('expired', [True, False])
def test_real_runtime_resolution_has_exact_terminal_and_replay(tmp_path, expired):
    async def case():
        clock = [10.0]
        physical = PhysicalToolCounter()
        runtime, uow, db = authorization_runtime(tmp_path / 'future.db',
            authorization=AuthorizationScenario(expires_at=11.0 if expired else None),
            physical=physical, owner_id='future-expiry', clock=lambda: clock[0])
        decision = await start_authorization_wait(runtime, uow)
        clock[0] = 12.0
        args = dict(decision_id=decision.decision_id, nonce=str(decision.request['nonce']),
                    expected_version=decision.version, decision=AuthorizationDecision.ALLOW if expired else AuthorizationDecision.DENY)
        await runtime.client.decide_authorization(RunId('run-fault'), **args)
        proof = uow.read_run_operation_audit(RunId('run-fault')).terminal_evidence
        assert proof is not None and proof.state == 'failed' and proof.created_at == 12.0
        metadata = uow.read_run_terminal_record(RunId('run-fault'))
        assert metadata.terminal_evidence == proof
        assert proof.matches(event_id=metadata.event_id, payload_hash=proof.event_payload_hash, state='failed')
        assert physical.calls == 0
        # Expiry committed DENY. Repeated late ALLOW preserves existing late rejection.
        args['decision'] = AuthorizationDecision.DENY
        await runtime.client.decide_authorization(RunId('run-fault'), **args)
        await runtime.client.cancel(RunId('run-fault'))
        assert uow.read_run_operation_audit(RunId('run-fault')).terminal_evidence == proof
        assert physical.calls == 0
        await runtime.close(); db.close()
        with Database.open(tmp_path / 'future.db') as reopened:
            assert SqliteExecutionUnitOfWork(reopened).read_run_operation_audit(RunId('run-fault')).terminal_evidence == proof
    asyncio.run(case())


def test_old_public_read_recovery_reopen_exact_and_rows_preserved(old_uow):
    uow = old_uow
    assert uow.read_run_operation_audit(RunId('run-fault')).terminal_evidence is None
    from simple_harness.execution.audit import RunAuditUnavailable
    with pytest.raises(RunAuditUnavailable, match='terminal_event_unavailable'):
        uow.read_run_terminal_record(RunId('run-fault'))
    witness = _read(uow)
    assert witness is not None and witness.original_resolved_at == 12.0
    before = _old_rows(uow.database)
    events = list(uow.database.connection.execute('SELECT * FROM run_events ORDER BY durable_seq'))
    proof = uow.recover_expired_authorization_terminal(witness, now=100.0)
    assert proof.created_at == 100.0 and proof.state == 'failed'
    metadata = uow.read_run_terminal_record(RunId('run-fault'))
    assert metadata.terminal_evidence == proof and metadata.error_code == 'authorization_expired'
    assert proof.matches(event_id=metadata.event_id, payload_hash=proof.event_payload_hash, state=proof.state)
    assert uow.read_run_operation_audit(RunId('run-fault')).terminal_evidence == proof
    assert _old_rows(uow.database) == before
    after = list(uow.database.connection.execute('SELECT * FROM run_events ORDER BY durable_seq'))
    assert [tuple(r) for r in after[:len(events)]] == [tuple(r) for r in events]
    assert len(after) == len(events) + 4  # two real events plus SDK same-TX witnesses
    payload = json.loads(after[-2]['payload_json'])
    assert payload['terminal_origin'] == 'authorization_expiry_recovery_v1'
    assert payload['original_resolved_at'] == 12.0 and payload['recovered_at'] == 100.0
    assert payload['source_hash'] == witness.source_hash
    with Database.open(uow.database.path, wal=True) as reopened:
        other = SqliteExecutionUnitOfWork(reopened)
        assert _read(other) == witness
        assert other.recover_expired_authorization_terminal(witness, now=200.0) == proof
    assert len(list(uow.database.connection.execute('SELECT * FROM run_events'))) == len(after)


@pytest.mark.parametrize('point', ['run.terminal_recovered.after_write', 'run.failed.before_write',
                                  'run.failed.after_write', 'authorization_terminal_recovery.after_commit'])
def test_recovery_fault_boundary_exact_reopen(old_uow, point):
    witness = _read(old_uow)
    before = _old_rows(old_uow.database)
    def fault(p):
        if p == point:
            raise RuntimeError('injected')
    with pytest.raises(RuntimeError, match='injected'):
        old_uow.recover_expired_authorization_terminal(witness, now=100.0, fault=fault)
    assert _old_rows(old_uow.database) == before
    with Database.open(old_uow.database.path, wal=True) as reopened:
        second = SqliteExecutionUnitOfWork(reopened)
        original = second.read_run_operation_audit(RunId('run-fault')).terminal_evidence
        assert (original is not None) == point.endswith('after_commit')
        proof = second.recover_expired_authorization_terminal(witness, now=200.0)
        assert proof.created_at == (100.0 if original else 200.0)
        if original:
            assert proof == original


@pytest.mark.parametrize('mutation', ['version', 'binding', 'tool_audit', 'extra_transition', 'terminal', 'unknown_audit'])
def test_stale_or_ambiguous_sources_refuse_no_writes(old_uow, mutation):
    witness = _read(old_uow)
    c = old_uow.database.connection
    if mutation == 'version':
        c.execute("UPDATE runs SET version=version+1")  # same updated_at is not a history proof
    elif mutation == 'binding':
        row = c.execute('SELECT response_json FROM decisions').fetchone()
        value = json.loads(row[0]); value['nonce'] = 'foreign'
        c.execute('UPDATE decisions SET response_json=?', (json.dumps(value, sort_keys=True, separators=(',', ':')),))
    elif mutation == 'tool_audit':
        c.execute("UPDATE run_events SET payload_json='{}' WHERE kind='audit.tool.v1'")
    else:
        old_uow._insert_event(c, event_id='negative-only', run_id='run-fault',
                              kind={'terminal': 'run.failed', 'unknown_audit': 'audit.unknown.v1'}.get(mutation, 'run.recovered'), payload={}, now=12.0)
    before = c.total_changes
    with pytest.raises(UnitOfWorkConflict):
        old_uow.recover_expired_authorization_terminal(witness, now=100.0)
    assert c.total_changes == before


def test_foreign_witness_and_invalid_time_refuse(old_uow):
    witness = _read(old_uow)
    before = old_uow.database.connection.total_changes
    for changed in (replace(witness, run_id='foreign'), replace(witness, decision_id='foreign'),
                    replace(witness, source_hash='0' * 64)):
        with pytest.raises(UnitOfWorkConflict):
            old_uow.recover_expired_authorization_terminal(changed, now=100.0)
    for now in (True, float('nan'), float('inf'), 11.0):
        with pytest.raises(ValueError):
            old_uow.recover_expired_authorization_terminal(witness, now=now)
    assert old_uow.database.connection.total_changes == before


def test_two_public_owners_share_one_recovery(old_uow):
    path = old_uow.database.path
    barrier = threading.Barrier(2)
    def owner(now):
        with Database.open(path, wal=True) as db:
            uow = SqliteExecutionUnitOfWork(db)
            witness = _read(uow)
            barrier.wait(timeout=5)
            return uow.recover_expired_authorization_terminal(witness, now=now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(owner, 100.0); b = pool.submit(owner, 200.0)
        assert a.result(timeout=10) == b.result(timeout=10)
    assert old_uow.database.connection.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.failed'").fetchone()[0] == 1


@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('point', ['decision_terminal.after_write', 'after_commit'])
def test_both_uow_terminal_branches_atomic(tmp_path, direct, point):
    """Command atomicity control; runtime identity is covered above separately."""
    path = tmp_path / 'command.db'
    with Database.open(path) as db:
        uow = SqliteExecutionUnitOfWork(db)
        uow.create_with_start_snapshot(execution_session_id='s', run_id='r', request_id='q',
            profile_key='agent.general', driver_kind='react', snapshot={}, event_id='r:created', now=1.0)
        request = {'control': 'unit-command'}
        if not direct:
            uow.commit_decision(decision_id='d', run_id='r', kind='tool_authorization', state=DecisionState.OPEN,
                request=request, response=None, event_id='d:open', now=2.0)
        prefix = 'decision' if direct else 'decision_resolve'
        target = prefix + '.after_commit' if point == 'after_commit' else point
        def fault(p):
            if p == target:
                raise RuntimeError('injected')
        with pytest.raises(RuntimeError, match='injected'):
            uow.commit_decision(decision_id='d', run_id='r', kind='tool_authorization', state=DecisionState.EXPIRED,
                request=request, response={'decision': 'deny'}, event_id='d:expired', now=3.0, fault=fault)
    with Database.open(path) as db:
        uow = SqliteExecutionUnitOfWork(db)
        proof = uow.read_run_operation_audit(RunId('r')).terminal_evidence
        assert (proof is not None) == (point == 'after_commit')
        uow.commit_decision(decision_id='d', run_id='r', kind='tool_authorization', state=DecisionState.EXPIRED,
            request=request, response={'decision': 'deny'}, event_id='d:expired', now=3.0)
        assert uow.read_run_operation_audit(RunId('r')).terminal_evidence is not None
        assert db.connection.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.failed'").fetchone()[0] == 1


@pytest.mark.parametrize('gate', ['active_owner', 'effect_exists'])
def test_active_owner_or_any_effect_refuses_recovery(old_uow, gate):
    witness = _read(old_uow)
    c = old_uow.database.connection
    if gate == 'active_owner':
        c.execute("INSERT INTO workflow_leases(run_id,namespace,owner_id,epoch,expires_at) "
                  "VALUES ('run-fault','runtime.kernel','negative-owner',9,1000) "
                  "ON CONFLICT(run_id,namespace) DO UPDATE SET expires_at=1000,owner_id='negative-owner',epoch=9")
    else:
        # Deliberately invalid legacy shape, never asserted to be a real handoff.
        c.execute("INSERT INTO execution_effects(effect_id,run_id,call_id,tool_name,arguments_json,"
                  "request_hash,authorization_receipt_ref,fence_epoch,state,prepared_at) "
                  "VALUES ('negative-effect','run-fault','negative-call','write_note','{}',?,'negative',1,'prepared',12)",
                  ('0' * 64,))
    before = c.total_changes
    with pytest.raises(UnitOfWorkConflict):
        old_uow.recover_expired_authorization_terminal(witness, now=100.0)
    assert c.total_changes == before
