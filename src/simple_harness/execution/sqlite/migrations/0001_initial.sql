-- SPDX-FileCopyrightText: 2026 DennyWanye
-- SPDX-License-Identifier: Apache-2.0

CREATE TABLE execution_sessions (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    execution_session_id TEXT NOT NULL REFERENCES execution_sessions(session_id),
    request_id TEXT NOT NULL,
    root_run_id TEXT NOT NULL,
    parent_run_id TEXT REFERENCES runs(run_id),
    profile_key TEXT NOT NULL,
    driver_kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'created', 'admission_pending', 'queued', 'running', 'waiting',
        'cancel_requested', 'completed', 'failed', 'cancelled'
    )),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    UNIQUE(execution_session_id, request_id),
    CHECK(parent_run_id IS NULL OR parent_run_id <> run_id)
) STRICT;

CREATE INDEX runs_session_state_idx ON runs(execution_session_id, state);
CREATE INDEX runs_parent_idx ON runs(parent_run_id);

CREATE TABLE run_start_snapshots (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    durable_seq INTEGER NOT NULL CHECK(durable_seq >= 1),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(run_id, durable_seq)
) STRICT;

CREATE TABLE run_admissions (
    admission_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('pending', 'allowed', 'denied', 'expired', 'cancelled')),
    prompt_json TEXT NOT NULL,
    response_json TEXT,
    expires_at REAL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    resolved_at REAL
) STRICT;

CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'allowed', 'denied', 'expired', 'cancelled')),
    request_json TEXT NOT NULL,
    response_json TEXT,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    resolved_at REAL,
    UNIQUE(run_id, decision_id)
) STRICT;

CREATE INDEX decisions_run_state_idx ON decisions(run_id, state);

CREATE TABLE continuations (
    continuation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    fifo_seq INTEGER NOT NULL CHECK(fifo_seq >= 1),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'acked', 'quarantined')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    claimed_by TEXT,
    runtime_lease_epoch INTEGER,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    ack_receipt_id TEXT UNIQUE REFERENCES continuation_progress_receipts(receipt_id),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    claimed_at REAL,
    acked_at REAL,
    UNIQUE(run_id, fifo_seq),
    CHECK((state = 'pending' AND claimed_by IS NULL AND runtime_lease_epoch IS NULL
             AND claimed_at IS NULL AND acked_at IS NULL AND claim_epoch = 0
             AND ack_receipt_id IS NULL)
       OR (state = 'claimed' AND claimed_by IS NOT NULL AND runtime_lease_epoch >= 1
             AND claimed_at IS NOT NULL AND acked_at IS NULL AND claim_epoch >= 1
             AND ack_receipt_id IS NULL)
       OR (state = 'acked' AND claimed_by IS NOT NULL AND runtime_lease_epoch >= 1
             AND claimed_at IS NOT NULL AND acked_at IS NOT NULL AND claim_epoch >= 1
             AND ack_receipt_id IS NOT NULL)
       OR (state = 'quarantined' AND acked_at IS NOT NULL))
) STRICT;

CREATE INDEX continuations_fifo_idx ON continuations(run_id, state, fifo_seq);

CREATE TABLE continuation_progress_receipts (
    receipt_id TEXT PRIMARY KEY,
    continuation_id TEXT NOT NULL UNIQUE REFERENCES continuations(continuation_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    claim_epoch INTEGER NOT NULL CHECK(claim_epoch >= 1),
    outcome_hash TEXT NOT NULL CHECK(length(outcome_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(continuation_id, owner_id, runtime_lease_epoch, claim_epoch)
) STRICT;

CREATE TABLE profile_launch_tickets (
    ticket_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    profile_key TEXT NOT NULL,
    catalog_generation INTEGER NOT NULL CHECK(catalog_generation >= 1),
    fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
    state TEXT NOT NULL CHECK(state IN ('issued', 'claimed', 'expired', 'cancelled')),
    child_run_id TEXT UNIQUE REFERENCES runs(run_id),
    issued_at REAL NOT NULL CHECK(issued_at >= 0),
    claimed_at REAL
) STRICT;

CREATE TABLE child_commands (
    command_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    ticket_id TEXT NOT NULL UNIQUE REFERENCES profile_launch_tickets(ticket_id),
    state TEXT NOT NULL CHECK(state IN ('pending', 'scheduled', 'acked', 'failed', 'cancelled')),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0)
) STRICT;

CREATE TABLE run_links (
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    child_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    attachment_policy TEXT NOT NULL,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    PRIMARY KEY(parent_run_id, child_run_id),
    CHECK(parent_run_id <> child_run_id)
) STRICT;

CREATE TABLE child_terminal_receipts (
    receipt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE REFERENCES child_commands(command_id) ON DELETE CASCADE,
    child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    terminal_state TEXT NOT NULL CHECK(terminal_state IN ('completed','failed','cancelled')),
    outcome_hash TEXT NOT NULL CHECK(length(outcome_hash) = 64),
    signal_id TEXT UNIQUE,
    event_id TEXT NOT NULL UNIQUE REFERENCES run_events(event_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    fence_epoch INTEGER NOT NULL CHECK(fence_epoch >= 1),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE child_signals (
    signal_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    child_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'acked')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    claimed_by TEXT,
    claimed_at REAL,
    claim_expires_at REAL,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    acked_at REAL,
    ack_receipt_id TEXT UNIQUE REFERENCES child_signal_ack_receipts(receipt_id),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    CHECK(
        (state = 'pending' AND claimed_by IS NULL AND claimed_at IS NULL
            AND claim_expires_at IS NULL AND claim_epoch = 0
            AND acked_at IS NULL AND ack_receipt_id IS NULL)
        OR
        (state = 'claimed' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL
            AND claim_expires_at > claimed_at AND claim_epoch >= 1
            AND acked_at IS NULL AND ack_receipt_id IS NULL)
        OR
        (state = 'acked' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL
            AND claim_expires_at > claimed_at AND claim_epoch >= 1
            AND acked_at IS NOT NULL AND ack_receipt_id IS NOT NULL)
    )
) STRICT;

CREATE INDEX child_signals_parent_head_idx
    ON child_signals(parent_run_id, created_at, signal_id, state);

CREATE TABLE child_signal_ack_receipts (
    receipt_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE REFERENCES child_signals(signal_id) ON DELETE CASCADE,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    claim_epoch INTEGER NOT NULL CHECK(claim_epoch >= 1),
    continuation_id TEXT NOT NULL UNIQUE REFERENCES continuations(continuation_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL UNIQUE REFERENCES run_events(event_id) ON DELETE CASCADE,
    continuation_payload_hash TEXT NOT NULL CHECK(length(continuation_payload_hash) = 64),
    event_payload_hash TEXT NOT NULL CHECK(length(event_payload_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(signal_id, owner_id, claim_epoch)
) STRICT;

CREATE TABLE workflow_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    checkpoint_hash TEXT NOT NULL CHECK(length(checkpoint_hash) = 64),
    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(run_id, namespace, version)
) STRICT;

CREATE TABLE workflow_leases (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch >= 1),
    expires_at REAL NOT NULL,
    PRIMARY KEY(run_id, namespace)
) STRICT;

CREATE TABLE workflow_operation_receipts (
    operation_id TEXT PRIMARY KEY,
    adapter_method TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    outcome_json TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_id TEXT,
    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE terminal_projection_prepares (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    terminal_checkpoint_id TEXT NOT NULL,
    descriptor_digest TEXT NOT NULL CHECK(length(descriptor_digest) = 64),
    input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
    output_json TEXT NOT NULL,
    output_hash TEXT NOT NULL CHECK(length(output_hash) = 64),
    blob_refs_json TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
    expected_head_id TEXT NOT NULL,
    consumed_at REAL,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(run_id, terminal_checkpoint_id, descriptor_digest)
) STRICT;

CREATE TABLE workflow_native_operations (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    base_checkpoint_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(run_id, namespace, base_checkpoint_id, operation_kind, identity_key)
) STRICT;

CREATE TABLE workflow_checkpoint_effect_links (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    effect_id TEXT NOT NULL REFERENCES execution_effects(effect_id),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    PRIMARY KEY(run_id, namespace, checkpoint_id, effect_id)
) STRICT;

CREATE TABLE workflow_decision_consumptions (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    checkpoint_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    response_json TEXT NOT NULL,
    consumed_at REAL NOT NULL CHECK(consumed_at >= 0),
    PRIMARY KEY(run_id, checkpoint_id, decision_id)
) STRICT;

CREATE TABLE provider_invocations (
    invocation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    request_json TEXT,
    target_json TEXT NOT NULL,
    target_digest TEXT NOT NULL CHECK(length(target_digest) = 64),
    estimator_json TEXT,
    estimator_digest TEXT CHECK(estimator_digest IS NULL OR length(estimator_digest) = 64),
    state TEXT NOT NULL CHECK(state IN ('claimed', 'handed_off', 'succeeded', 'failed', 'unknown')),
    response_json TEXT,
    usage_json TEXT,
    error_code TEXT,
    claimed_at REAL NOT NULL CHECK(claimed_at >= 0),
    handed_off_at REAL,
    settled_at REAL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    handoff_attempt INTEGER NOT NULL DEFAULT 0 CHECK(handoff_attempt >= 0),
    rehandoff_count INTEGER NOT NULL DEFAULT 0 CHECK(rehandoff_count BETWEEN 0 AND 1),
    UNIQUE(run_id, request_id),
    CHECK((estimator_json IS NULL) = (estimator_digest IS NULL))
) STRICT;

CREATE INDEX provider_invocations_run_state_idx ON provider_invocations(run_id, state);

CREATE TABLE run_fences (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    epoch INTEGER NOT NULL CHECK(epoch >= 1),
    state TEXT NOT NULL CHECK(state IN ('active', 'released', 'cancelled')),
    acquired_at REAL NOT NULL CHECK(acquired_at >= 0),
    released_at REAL
) STRICT;

CREATE TABLE execution_effects (
    effect_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    raw_call_id TEXT,
    turn_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(turn_ordinal >= 0),
    call_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(call_ordinal >= 0),
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    authorization_receipt_ref TEXT NOT NULL,
    handoff_receipt_ref TEXT,
    evidence_ref TEXT,
    fence_epoch INTEGER NOT NULL CHECK(fence_epoch >= 1),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'handed_off', 'succeeded', 'partial', 'rejected', 'failed', 'unknown')),
    result_json TEXT,
    prepared_at REAL NOT NULL CHECK(prepared_at >= 0),
    handed_off_at REAL,
    settled_at REAL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    handoff_attempt INTEGER NOT NULL DEFAULT 0 CHECK(handoff_attempt >= 0),
    rehandoff_count INTEGER NOT NULL DEFAULT 0 CHECK(rehandoff_count BETWEEN 0 AND 1),
    UNIQUE(run_id, call_id, effect_id)
) STRICT;

CREATE INDEX execution_effects_run_state_idx ON execution_effects(run_id, state);

CREATE TABLE reconciliation_resolutions (
    resolution_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('provider', 'tool')),
    ledger_identity TEXT NOT NULL,
    handoff_attempt INTEGER NOT NULL CHECK(handoff_attempt >= 1),
    outcome TEXT NOT NULL CHECK(outcome IN ('completed', 'confirmed_not_started')),
    outcome_hash TEXT NOT NULL CHECK(length(outcome_hash) = 64),
    evidence_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(kind, ledger_identity, handoff_attempt)
) STRICT;

CREATE TABLE run_wait_blockers (
    blocker_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('provider', 'tool')),
    ledger_identity TEXT NOT NULL,
    handoff_attempt INTEGER NOT NULL CHECK(handoff_attempt >= 1),
    observed_version INTEGER NOT NULL CHECK(observed_version >= 1),
    resolution_id TEXT REFERENCES reconciliation_resolutions(resolution_id),
    wake_consumed INTEGER NOT NULL DEFAULT 0 CHECK(wake_consumed IN (0, 1)),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    resolved_at REAL,
    consumed_at REAL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    UNIQUE(kind, ledger_identity, handoff_attempt)
) STRICT;

CREATE INDEX run_wait_blockers_wake_idx
ON run_wait_blockers(run_id, wake_consumed, resolution_id);

CREATE TABLE wait_activation_receipts (
    receipt_id TEXT PRIMARY KEY,
    blocker_id TEXT NOT NULL UNIQUE REFERENCES run_wait_blockers(blocker_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    outcome_hash TEXT NOT NULL CHECK(length(outcome_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE delivery_outbox (
    delivery_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    sink_kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'delivered', 'failed', 'released')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    claimed_at REAL,
    settled_at REAL
) STRICT;

CREATE INDEX delivery_outbox_state_idx ON delivery_outbox(state, created_at);
