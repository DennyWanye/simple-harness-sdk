"""Decision terminal commits and narrowly proved pre-H077 expiry recovery.

Never called by database open. All mutation is owned by the public UoW command.
"""
from __future__ import annotations

import hashlib
import json
import math

from simple_harness.contracts import canonical_json
from simple_harness.execution.audit import audit_hash
from simple_harness.execution.decision_recovery import ExpiredAuthorizationTerminalRecoveryV1
from simple_harness.execution.effects import effect_request_hash
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.tools.authorization import sdk_authorization_receipt

_TERMINALS = ('run.completed', 'run.failed', 'run.cancelled')
_ORIGIN = 'authorization_expiry_recovery_v1'


def _reject():
    raise UnitOfWorkConflict('authorization_terminal_source_unverifiable')


def _object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        _reject()
    if not isinstance(value, dict) or canonical_json(value) != raw:
        _reject()
    return value


def _hash(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def append_decision_terminal(uow, connection, *, run_id, decision_id, event_id, now):
    """Only ordinary root React tool authorization owns this terminal domain."""
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    decision = connection.execute('SELECT * FROM decisions WHERE decision_id=?', (decision_id,)).fetchone()
    if (run['parent_run_id'] is not None or run['driver_kind'] != 'react'
            or decision['kind'] != 'tool_authorization'
            or decision['state'] not in {'denied', 'expired', 'cancelled'}):
        return
    if connection.execute("SELECT 1 FROM run_events WHERE run_id=? AND kind IN "
                          "('run.failed','run.cancelled','run.completed')", (run_id,)).fetchone():
        raise UnitOfWorkConflict('decision_terminal_already_exists')
    state = 'cancelled' if decision['state'] == 'cancelled' else 'failed'
    if run['state'] != state:
        _reject()
    payload = dict(code='authorization_' + decision['state'],
                   terminal_origin='authorization_decision_v1',
                   decision_id=decision_id, decision_event_id=event_id,
                   decision_request_hash=_hash(decision['request_json']),
                   decision_response_hash=_hash(decision['response_json']),
                   run_version=run['version'])
    uow._insert_event(connection, event_id=event_id + ':run-terminal:v1', run_id=run_id,
                      kind='run.' + state, payload=payload, now=now)


def _witness_event(connection, event):
    """Original same-transaction event witness, not a caller's hash assertion."""
    matches = []
    for row in connection.execute("SELECT * FROM run_events WHERE run_id=? AND kind='audit.event.v2'",
                                  (event['run_id'],)):
        value = _object(row['payload_json'])
        if value.get('source_id') != event['event_id']:
            continue
        if (value.get('source_hash') != audit_hash(dict(event))
                or value.get('source_sequence') != event['durable_seq']
                or value.get('contract') != 'sdk.core.v2'
                or row['event_id'] != 'audit-event:' + audit_hash(value)):
            _reject()
        matches.append(dict(row))
    if len(matches) != 1:
        _reject()
    return matches[0]


def eligible(connection, run_id):
    """Exact first-authorization legacy shape. Unknown histories fail closed."""
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None or run['state'] != 'failed':
        return None
    if (run['parent_run_id'] is not None or run['root_run_id'] != run_id
            or run['driver_kind'] != 'react'):
        _reject()
    rows = connection.execute('SELECT * FROM decisions WHERE run_id=? LIMIT 2', (run_id,)).fetchall()
    if len(rows) != 1:
        _reject()
    decision = rows[0]
    if (decision['kind'] != 'tool_authorization' or decision['state'] != 'expired'
            or decision['version'] != 1):
        _reject()
    request = _object(decision['request_json'])
    response = _object(decision['response_json'])
    deadline = request.get('expires_at')
    resolved = decision['resolved_at']
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(resolved) not in (int, float) or not math.isfinite(resolved)
            or not 0 <= decision['created_at'] <= deadline <= resolved
            or run['updated_at'] != resolved
            or response.get('decision') != 'deny'
            or not isinstance(request.get('nonce'), str) or not request['nonce']
            or response.get('nonce') != request['nonce']):
        _reject()
    effect_id, call_id, tool_name = (request.get(k) for k in ('effect_id', 'call_id', 'tool_name'))
    if (any(not isinstance(v, str) or not v for v in (effect_id, call_id, tool_name))
            or decision['decision_id'] != 'authorization:' + effect_id
            or not isinstance(request.get('arguments'), dict)):
        _reject()
    try:
        prefix, digest, body = response['authorization_receipt_ref'].split(':', 2)
        binding = _object(body)
    except (KeyError, AttributeError, ValueError):
        _reject()
    expected = sdk_authorization_receipt('decision', dict(
        decision='deny', decision_id=decision['decision_id'], decision_version=0,
        effect_id=effect_id, nonce=request['nonce'], run_id=run_id))
    if (prefix != 'authorization-binding-v1' or digest != _hash(body)
            or set(binding) != {'host_receipt_hash', 'host_receipt_ref', 'sdk_receipt_hash', 'sdk_receipt_ref'}
            or binding['sdk_receipt_hash'] != expected.receipt_hash
            or binding['sdk_receipt_ref'] != expected.receipt_ref
            or not isinstance(binding['host_receipt_ref'], str) or not binding['host_receipt_ref']
            or not isinstance(binding['host_receipt_hash'], str)
            or len(binding['host_receipt_hash']) != 64
            or any(c not in '0123456789abcdef' for c in binding['host_receipt_hash'])):
        _reject()
    # Require the whole *original* business transition sequence, not version==3
    # alone. Known legacy commands: create(v0), activate(+1), open(+1), expire(+1).
    # Any extra/missing/reordered transition or later state write fails this shape.
    all_events = connection.execute('SELECT * FROM run_events WHERE run_id=? ORDER BY durable_seq LIMIT 4097',
                                    (run_id,)).fetchall()
    if len(all_events) > 4096:
        _reject()
    allowed_audits = {'audit.event.v2', 'audit.runtime.v2', 'audit.transition.v1', 'audit.tool.v1'}
    if any(r['kind'].startswith('audit.') and r['kind'] not in allowed_audits for r in all_events):
        _reject()
    original = [r for r in all_events if not r['kind'].startswith('audit.')
                and r['kind'] not in (*_TERMINALS, 'run.terminal_recovered')]
    if [r['kind'] for r in original] != ['run.created', 'run.activated', 'decision.open', 'decision.expired']:
        _reject()
    created, activated, opened, expired = original
    if (created['event_id'] != run_id + ':created' or created['created_at'] != run['created_at']
            or opened['event_id'] != decision['decision_id'] + ':open'
            or expired['event_id'] != decision['decision_id'] + ':expired:1'
            or opened['created_at'] != decision['created_at'] or expired['created_at'] != resolved
            or not created['created_at'] <= activated['created_at'] <= opened['created_at'] <= resolved
            or _object(created['payload_json']) != dict(driver_kind='react', profile_key=run['profile_key'])
            or _object(opened['payload_json']) != dict(decision_id=decision['decision_id'], kind='tool_authorization')
            or expired['payload_json'] != opened['payload_json']):
        _reject()
    activation = _object(activated['payload_json'])
    if (set(activation) != {'lease_epoch', 'owner_id'} or type(activation['lease_epoch']) is not int or activation['lease_epoch'] != 1
            or not isinstance(activation['owner_id'], str)
            or activated['event_id'] != f"{run_id}:runtime:runtime.kernel:1:activated"
            or run['version'] != sum({'run.created': 0, 'run.activated': 1,
                                     'decision.open': 1, 'decision.expired': 1}[r['kind']] for r in original)):
        _reject()
    event_witnesses = [_witness_event(connection, event) for event in original]
    # REQUIRE_USER is pre-effect admission. Absence alone is insufficient: pair
    # the immutable requested/waiting audit records to exact original call bytes.
    if connection.execute('SELECT 1 FROM execution_effects WHERE run_id=? OR effect_id=? LIMIT 1',
                          (run_id, effect_id)).fetchone():
        _reject()
    tool_events = [r for r in all_events if r['kind'] == 'audit.tool.v1']
    if len(tool_events) != 2:
        _reject()
    for row, state in zip(tool_events, ('requested', 'waiting'), strict=True):
        value = _object(row['payload_json'])
        if (value.get('state') != state or value.get('effect_id') != effect_id
                or value.get('call_id') != call_id or value.get('operation_id') != 'tool:' + effect_id
                or value.get('operation_name_hash') != audit_hash(tool_name)
                or value.get('request_hash') != effect_request_hash(tool_name=tool_name, arguments=request['arguments'])
                or not activated['durable_seq'] < row['durable_seq'] < opened['durable_seq']):
            _reject()
        identity = 'audit-tool:' + audit_hash(dict(run_id=run_id, operation_id=value['operation_id'], state=state))
        if state != 'requested':
            identity += ':' + audit_hash(value)
        if row['event_id'] != identity + ':' + audit_hash(value):
            _reject()
    snapshot = connection.execute('SELECT * FROM run_start_snapshots WHERE run_id=?', (run_id,)).fetchone()
    if snapshot is None or _hash(snapshot['snapshot_json']) != snapshot['snapshot_hash']:
        _reject()
    snapshot_body = _object(snapshot['snapshot_json'])
    if (snapshot_body.get('driver_kind') != 'react' or snapshot_body.get('profile_key') != run['profile_key']
            or snapshot['created_at'] != run['created_at']):
        _reject()
    source = dict(run=dict(run), decision=dict(decision), events=[dict(r) for r in original],
                  event_witnesses=event_witnesses, tool_events=[dict(r) for r in tool_events],
                  start_snapshot_hash=snapshot['snapshot_hash'], contract=_ORIGIN)
    witness = ExpiredAuthorizationTerminalRecoveryV1(run_id, decision['decision_id'], run['version'],
                                                     audit_hash(source), resolved)
    existing = [r for r in all_events if r['kind'] in (*_TERMINALS, 'run.terminal_recovered')]
    _existing_recovery(existing, witness, source)
    for event in existing:
        _witness_event(connection, event)
    return witness, source


def _recovery_payload(witness, source):
    recovery_id = 'authorization-terminal-recovery:' + witness.source_hash + ':audit'
    return dict(terminal_origin=_ORIGIN, code='authorization_expired',
                decision_id=witness.decision_id, source_hash=witness.source_hash,
                original_resolved_at=witness.original_resolved_at, run_version=witness.run_version,
                recovery_event_id=recovery_id,
                source_commitments=dict(
                    decision_request_hash=_hash(source['decision']['request_json']),
                    decision_response_hash=_hash(source['decision']['response_json']),
                    start_snapshot_hash=source['start_snapshot_hash'],
                    original_event_hashes=[audit_hash(r) for r in source['events']],
                    tool_audit_hashes=[audit_hash(r) for r in source['tool_events']] ))


def _existing_recovery(events, witness, source):
    if not events:
        return
    terminal_id = 'authorization-terminal-recovery:' + witness.source_hash
    if len(events) != 2 or [(r['event_id'], r['kind']) for r in events] != [
            (terminal_id + ':audit', 'run.terminal_recovered'), (terminal_id, 'run.failed')]:
        _reject()
    recorded_at = events[0]['created_at']
    if (not math.isfinite(recorded_at) or recorded_at < witness.original_resolved_at
            or events[1]['created_at'] != recorded_at
            or _object(events[0]['payload_json']) != {**_recovery_payload(witness, source), 'recovered_at': recorded_at}
            or events[1]['payload_json'] != events[0]['payload_json']):
        _reject()


def recover(uow, connection, witness, *, now, fault=None):
    if type(witness) is not ExpiredAuthorizationTerminalRecoveryV1:
        raise TypeError('typed expiry recovery witness required')
    if type(now) not in (int, float) or not math.isfinite(now) or now < witness.original_resolved_at:
        raise ValueError('recovery time must be finite and at least original resolution time')
    found = eligible(connection, witness.run_id)
    if found is None or found[0] != witness:
        _reject()
    run_id = witness.run_id
    if connection.execute('SELECT 1 FROM workflow_leases WHERE run_id=? AND expires_at>? LIMIT 1',
                          (run_id, now)).fetchone():
        raise UnitOfWorkConflict('authorization_terminal_runtime_owner_active')
    terminal_id = 'authorization-terminal-recovery:' + witness.source_hash
    recovery_id = terminal_id + ':audit'
    events = connection.execute("SELECT * FROM run_events WHERE run_id=? AND kind IN "
        "('run.failed','run.completed','run.cancelled','run.terminal_recovered') ORDER BY durable_seq", (run_id,)).fetchall()
    proof = _recovery_payload(witness, found[1])
    if events:
        _existing_recovery(events, witness, found[1])
        for event in events:
            _witness_event(connection, event)
    else:
        payload = {**proof, 'recovered_at': now}
        for event_id, kind in ((recovery_id, 'run.terminal_recovered'), (terminal_id, 'run.failed')):
            if fault:
                fault(kind + '.before_write')
            uow._insert_event(connection, event_id=event_id, run_id=run_id, kind=kind, payload=payload, now=now)
            if fault:
                fault(kind + '.after_write')
    from .audit import terminal_evidence
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    return terminal_evidence(connection, run)
