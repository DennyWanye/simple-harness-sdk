-- SPDX-FileCopyrightText: 2026 DennyWanye
-- SPDX-License-Identifier: Apache-2.0

-- The only accepted fresh execution schema v3 descriptor. This file is
-- self-contained: it preserves every v1/v2 authority and adds conversation
-- ownership, durable context staging, and the Memory outbox. It is never
-- applied as a migration to an existing database.

CREATE TABLE execution_users (
    user_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE execution_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'harness-system' REFERENCES execution_users(user_id),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(session_id, user_id)
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
        'cancel_requested', 'reserved_fork', 'completed', 'failed', 'cancelled'
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
    ticket_id TEXT UNIQUE REFERENCES profile_launch_tickets(ticket_id),
    workflow_ticket_receipt_id TEXT UNIQUE
        REFERENCES workflow_launch_ticket_receipts(ticket_receipt_id),
    state TEXT NOT NULL CHECK(state IN ('pending', 'scheduled', 'acked', 'failed', 'cancelled')),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    CHECK((ticket_id IS NOT NULL) <> (workflow_ticket_receipt_id IS NOT NULL))
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

CREATE TABLE workflow_catalog_authorities (
    authority_id TEXT PRIMARY KEY CHECK(authority_id = 'model_spawnable'),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    version INTEGER NOT NULL CHECK(version >= 0),
    catalog_hash TEXT NOT NULL CHECK(length(catalog_hash) = 64),
    canonical_profiles TEXT NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= 0)
) STRICT;

CREATE TABLE workflow_launch_ticket_receipts (
    ticket_receipt_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL UNIQUE,
    canonical_payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    candidate_id TEXT,
    profile_key TEXT NOT NULL,
    catalog_generation INTEGER NOT NULL CHECK(catalog_generation >= 1),
    catalog_authority_version INTEGER NOT NULL CHECK(catalog_authority_version >= 0),
    catalog_hash TEXT NOT NULL CHECK(length(catalog_hash) = 64),
    profile_fingerprint TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    implementation_fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    requested_run_id TEXT,
    requested_trace_id TEXT,
    requested_thread_id TEXT,
    resolved_run_id TEXT NOT NULL,
    resolved_trace_id TEXT NOT NULL,
    resolved_thread_id TEXT NOT NULL,
    tool_catalog_generation INTEGER NOT NULL CHECK(tool_catalog_generation >= 1),
    objective TEXT NOT NULL,
    objective_hash TEXT NOT NULL CHECK(length(objective_hash) = 64),
    start_input_hash TEXT NOT NULL CHECK(length(start_input_hash) = 64),
    spawn_origin_json TEXT NOT NULL,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id),
    root_run_id TEXT NOT NULL REFERENCES runs(run_id),
    attachment_policy TEXT NOT NULL CHECK(attachment_policy='attached'),
    child_command_id TEXT NOT NULL,
    issue_authority_hash TEXT NOT NULL CHECK(length(issue_authority_hash) = 64),
    issued_at REAL NOT NULL CHECK(issued_at >= 0)
) STRICT;

CREATE TABLE runtime_start_receipts (
    ticket_receipt_id TEXT PRIMARY KEY REFERENCES workflow_launch_ticket_receipts(ticket_receipt_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    trace_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    committed_run_version INTEGER NOT NULL CHECK(committed_run_version >= 0),
    start_snapshot_hash TEXT NOT NULL CHECK(length(start_snapshot_hash) = 64),
    workflow_request_hash TEXT NOT NULL CHECK(length(workflow_request_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE runtime_start_dispatch_claims (
    claim_id TEXT PRIMARY KEY,
    ticket_receipt_id TEXT NOT NULL UNIQUE REFERENCES runtime_start_receipts(ticket_receipt_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    claim_epoch INTEGER NOT NULL CHECK(claim_epoch >= 1),
    expires_at REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    state TEXT NOT NULL CHECK(state IN ('claimed','consumed')),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0)
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

CREATE TABLE workflow_start_admissions (
    request_key TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    request_json TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('standalone','precreated')),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    trace_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('admitted','claimed','running','settled')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    claim_action TEXT CHECK(claim_action IN ('new','resume')),
    claim_owner TEXT,
    claim_epoch INTEGER,
    claim_expires_at REAL CHECK(claim_expires_at >= 0),
    outcome_json TEXT,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    CHECK((phase='admitted' AND claim_action IS NULL AND claim_owner IS NULL
            AND claim_epoch IS NULL AND claim_expires_at IS NULL AND outcome_json IS NULL)
       OR (phase IN ('claimed','running') AND claim_action IS NOT NULL
            AND claim_owner IS NOT NULL AND claim_epoch >= 1
            AND claim_expires_at IS NOT NULL AND outcome_json IS NULL)
       OR (phase='settled' AND claim_action IS NOT NULL AND claim_owner IS NOT NULL
            AND claim_epoch >= 1 AND claim_expires_at IS NOT NULL
            AND outcome_json IS NOT NULL)),
    CHECK(claim_action IS NULL OR claim_action='new' OR version >= 1)
) STRICT;

CREATE TABLE workflow_resume_admissions (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    request_json TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('standalone','precreated')),
    expected_run_version INTEGER NOT NULL CHECK(expected_run_version >= 0),
    expected_checkpoint_head TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('admitted','claimed','retry_wait','settled')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    claim_owner TEXT,
    claim_epoch INTEGER,
    claim_expires_at REAL,
    retry_attempt INTEGER NOT NULL DEFAULT 0 CHECK(retry_attempt >= 0),
    next_attempt_at REAL,
    committed_checkpoint TEXT,
    outcome_json TEXT,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    UNIQUE(run_id, request_fingerprint)
) STRICT;

CREATE TABLE workflow_cancel_receipts (
    cancel_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    reason TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('requested','cancelling','blocked','terminal')),
    blocker_ids_json TEXT NOT NULL,
    blocker_snapshot_json TEXT NOT NULL,
    terminal INTEGER CHECK(terminal IS NULL OR terminal IN (0,1)),
    convergence_owner TEXT,
    convergence_epoch INTEGER NOT NULL DEFAULT 0 CHECK(convergence_epoch >= 0),
    convergence_expires_at REAL,
    outcome_json TEXT,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    UNIQUE(run_id, generation)
) STRICT;

CREATE TABLE workflow_recovery_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    candidate_hash TEXT NOT NULL CHECK(length(candidate_hash) = 64),
    snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
    previous_status TEXT NOT NULL,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE workflow_recovery_claims (
    blocker_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    resolution_version INTEGER NOT NULL CHECK(resolution_version >= 0),
    owner_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch >= 1),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0)
) STRICT;

CREATE TABLE workflow_fork_receipts (
    fork_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
    request_json TEXT NOT NULL,
    source_run_id TEXT NOT NULL REFERENCES runs(run_id),
    source_namespace TEXT NOT NULL,
    source_checkpoint_id TEXT NOT NULL,
    source_run_version INTEGER NOT NULL CHECK(source_run_version >= 0),
    source_head TEXT NOT NULL,
    target_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    target_trace_id TEXT NOT NULL UNIQUE,
    target_thread_id TEXT NOT NULL UNIQUE,
    target_checkpoint_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('prepared','claimed','checkpointed','committed','rolled_back')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at REAL,
    target_checkpoint_hash TEXT,
    outcome_json TEXT,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0)
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

CREATE TABLE workflow_spawn_continuations (
    operation_id TEXT PRIMARY KEY,
    ticket_receipt_id TEXT NOT NULL UNIQUE
        REFERENCES workflow_launch_ticket_receipts(ticket_receipt_id),
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('pending','claimed','completed')),
    owner_id TEXT,
    runtime_lease_epoch INTEGER CHECK(runtime_lease_epoch >= 1),
    run_fence_epoch INTEGER CHECK(run_fence_epoch >= 1),
    workflow_lease_epoch INTEGER CHECK(workflow_lease_epoch >= 1),
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    expires_at REAL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    completion_receipt_id TEXT UNIQUE,
    completion_path_kind TEXT,
    effect_id TEXT NOT NULL REFERENCES execution_effects(effect_id),
    handoff_attempt INTEGER NOT NULL CHECK(handoff_attempt >= 1),
    effect_request_hash TEXT NOT NULL CHECK(length(effect_request_hash) = 64),
    issue_authority_hash TEXT NOT NULL CHECK(length(issue_authority_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    CHECK((state='pending' AND owner_id IS NULL AND runtime_lease_epoch IS NULL
           AND run_fence_epoch IS NULL AND workflow_lease_epoch IS NULL
           AND claim_epoch=0 AND expires_at IS NULL AND completion_receipt_id IS NULL)
       OR (state='claimed' AND owner_id IS NOT NULL AND runtime_lease_epoch IS NOT NULL
           AND run_fence_epoch IS NOT NULL AND claim_epoch >= 1 AND expires_at IS NOT NULL
           AND completion_receipt_id IS NULL)
       OR (state='completed' AND completion_receipt_id IS NOT NULL
           AND completion_path_kind IS NOT NULL))
) STRICT;

CREATE TABLE workflow_spawn_continuation_ready (
    ready_receipt_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE
        REFERENCES workflow_spawn_continuations(operation_id) ON DELETE CASCADE,
    ticket_receipt_id TEXT NOT NULL
        REFERENCES workflow_launch_ticket_receipts(ticket_receipt_id),
    effect_id TEXT NOT NULL REFERENCES execution_effects(effect_id),
    handoff_attempt INTEGER NOT NULL CHECK(handoff_attempt >= 1),
    evidence_ref TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    consumed_at REAL
) STRICT;

CREATE TABLE workflow_spawn_ready_activations (
    activation_receipt_id TEXT PRIMARY KEY,
    ready_receipt_id TEXT NOT NULL
        REFERENCES workflow_spawn_continuation_ready(ready_receipt_id) ON DELETE CASCADE,
    spawn_operation_id TEXT NOT NULL
        REFERENCES workflow_spawn_continuations(operation_id) ON DELETE CASCADE,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    effect_id TEXT NOT NULL REFERENCES execution_effects(effect_id),
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    run_fence_epoch INTEGER NOT NULL CHECK(run_fence_epoch >= 1),
    workflow_lease_epoch INTEGER CHECK(workflow_lease_epoch >= 1),
    continuation_claim_epoch INTEGER NOT NULL CHECK(continuation_claim_epoch >= 1),
    predecessor_activation_receipt_id TEXT UNIQUE
        REFERENCES workflow_spawn_ready_activations(activation_receipt_id),
    state TEXT NOT NULL CHECK(state IN ('active','superseded','consumed')),
    version INTEGER NOT NULL CHECK(version >= 1),
    canonical_hash TEXT NOT NULL CHECK(length(canonical_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    superseded_at REAL,
    consumed_at REAL,
    CHECK((state='active' AND superseded_at IS NULL AND consumed_at IS NULL)
       OR (state='superseded' AND superseded_at IS NOT NULL AND consumed_at IS NULL)
       OR (state='consumed' AND consumed_at IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX workflow_spawn_ready_one_active_idx
ON workflow_spawn_ready_activations(ready_receipt_id) WHERE state='active';

CREATE INDEX workflow_spawn_ready_parent_idx
ON workflow_spawn_ready_activations(parent_run_id, state, created_at);

CREATE TABLE workflow_spawn_child_wait_receipts (
    parent_wait_receipt_id TEXT PRIMARY KEY,
    spawn_operation_id TEXT NOT NULL UNIQUE
        REFERENCES workflow_spawn_continuations(operation_id) ON DELETE CASCADE,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    child_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    child_command_id TEXT NOT NULL REFERENCES child_commands(command_id),
    parent_pre_version INTEGER NOT NULL CHECK(parent_pre_version >= 0),
    parent_waiting_version INTEGER NOT NULL CHECK(parent_waiting_version >= 1),
    react_checkpoint_revision INTEGER NOT NULL CHECK(react_checkpoint_revision >= 0),
    react_checkpoint_hash TEXT NOT NULL CHECK(length(react_checkpoint_hash) = 64),
    expected_signal_domain TEXT NOT NULL,
    source_phase TEXT NOT NULL CHECK(source_phase='tool_batch_reserved'),
    batch_digest TEXT NOT NULL CHECK(length(batch_digest) = 64),
    spawn_ordinal INTEGER NOT NULL CHECK(spawn_ordinal >= 0),
    next_tool_ordinal INTEGER NOT NULL CHECK(next_tool_ordinal >= 0),
    prior_result_append_receipts_json TEXT NOT NULL,
    budget_terminal_code TEXT,
    synthetic_result_append_receipts_json TEXT,
    raw_tool_call_id TEXT NOT NULL,
    spawn_result_append_id TEXT NOT NULL,
    spawn_result_append_receipt_id TEXT NOT NULL,
    spawn_tool_message_hash TEXT NOT NULL CHECK(length(spawn_tool_message_hash) = 64),
    context_pre_revision INTEGER NOT NULL CHECK(context_pre_revision >= 0),
    context_post_revision INTEGER NOT NULL CHECK(context_post_revision >= 0),
    released_runtime_lease_epoch INTEGER NOT NULL CHECK(released_runtime_lease_epoch >= 1),
    released_workflow_lease_epoch INTEGER CHECK(released_workflow_lease_epoch >= 1),
    termination_started_at REAL NOT NULL CHECK(termination_started_at >= 0),
    termination_last_observed_at REAL NOT NULL CHECK(termination_last_observed_at >= 0),
    wall_deadline REAL,
    termination_policy_snapshot_hash TEXT NOT NULL CHECK(length(termination_policy_snapshot_hash) = 64),
    released_run_fence_epoch INTEGER NOT NULL CHECK(released_run_fence_epoch >= 1),
    child_start_receipt_id TEXT NOT NULL,
    child_dispatch_claim_id TEXT NOT NULL,
    child_runtime_lease_epoch INTEGER NOT NULL CHECK(child_runtime_lease_epoch >= 1),
    state TEXT NOT NULL CHECK(state IN (
        'unconsumed','woken','claimed','acked_completion_pending','acked','acked_parent_terminal'
    )),
    child_signal_id TEXT,
    continuation_id TEXT REFERENCES continuations(continuation_id),
    wake_activation_receipt_id TEXT REFERENCES workflow_spawn_ready_activations(activation_receipt_id),
    progress_receipt_id TEXT REFERENCES continuation_progress_receipts(receipt_id),
    pending_child_completion_json TEXT,
    pending_child_completion_hash TEXT CHECK(pending_child_completion_hash IS NULL OR length(pending_child_completion_hash)=64),
    child_completion_append_id TEXT,
    child_completion_append_receipt_id TEXT,
    child_completion_context_revision INTEGER CHECK(child_completion_context_revision >= 0),
    pending_completion_terminal_receipt_id TEXT,
    pending_completion_terminal_state TEXT,
    pending_completion_terminal_hash TEXT CHECK(pending_completion_terminal_hash IS NULL OR length(pending_completion_terminal_hash)=64),
    parent_terminal_phase_kind TEXT,
    child_cancel_request_id TEXT,
    child_cancel_receipt_id TEXT,
    reused_child_cancel_receipt_id TEXT,
    late_signal_quarantine_receipt_id TEXT,
    claimed_continuation_terminal_ack_receipt_id TEXT,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    identity_hash TEXT NOT NULL CHECK(length(identity_hash) = 64),
    lifecycle_hash TEXT NOT NULL CHECK(length(lifecycle_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE workflow_spawn_completion_receipts (
    completion_receipt_id TEXT PRIMARY KEY,
    spawn_operation_id TEXT NOT NULL UNIQUE
        REFERENCES workflow_spawn_continuations(operation_id) ON DELETE CASCADE,
    ticket_receipt_id TEXT NOT NULL
        REFERENCES workflow_launch_ticket_receipts(ticket_receipt_id),
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    path_kind TEXT NOT NULL CHECK(path_kind IN (
        'direct','ready_recovery','parent_terminal_ticket_only',
        'parent_terminal_ready_unactivated','parent_terminal_activated'
    )),
    effect_id TEXT NOT NULL REFERENCES execution_effects(effect_id),
    handoff_attempt INTEGER NOT NULL CHECK(handoff_attempt >= 1),
    effect_request_hash TEXT NOT NULL CHECK(length(effect_request_hash) = 64),
    issue_authority_hash TEXT NOT NULL CHECK(length(issue_authority_hash) = 64),
    tool_result_json TEXT NOT NULL,
    tool_result_hash TEXT NOT NULL CHECK(length(tool_result_hash) = 64),
    child_runtime_start_receipt_id TEXT REFERENCES runtime_start_receipts(ticket_receipt_id),
    failure_evidence_kind TEXT,
    failure_evidence_id TEXT,
    failure_evidence_json TEXT,
    failure_evidence_hash TEXT CHECK(failure_evidence_hash IS NULL OR length(failure_evidence_hash)=64),
    activation_chain_head_id TEXT REFERENCES workflow_spawn_ready_activations(activation_receipt_id),
    child_wait_receipt_id TEXT UNIQUE
        REFERENCES workflow_spawn_child_wait_receipts(parent_wait_receipt_id),
    canonical_hash TEXT NOT NULL CHECK(length(canonical_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

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
    superseded_by TEXT REFERENCES workflow_spawn_continuation_ready(ready_receipt_id),
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

CREATE TABLE workflow_terminal_fence_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    runtime_lease_epoch INTEGER NOT NULL CHECK(runtime_lease_epoch >= 1),
    run_fence_epoch INTEGER NOT NULL CHECK(run_fence_epoch >= 1),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE workflow_terminal_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    checkpoint_id TEXT NOT NULL,
    checkpoint_namespace TEXT NOT NULL,
    checkpoint_version INTEGER NOT NULL CHECK(checkpoint_version >= 0),
    checkpoint_hash TEXT NOT NULL CHECK(length(checkpoint_hash) = 64),
    state TEXT NOT NULL CHECK(state IN ('completed','failed','cancelled')),
    run_version INTEGER NOT NULL CHECK(run_version >= 1),
    event_id TEXT NOT NULL UNIQUE REFERENCES run_events(event_id),
    event_payload_hash TEXT NOT NULL CHECK(length(event_payload_hash) = 64),
    delivery_ids_json TEXT NOT NULL,
    delivery_facts_json TEXT NOT NULL,
    terminal_payload_json TEXT NOT NULL,
    terminal_fence_receipt_id TEXT NOT NULL UNIQUE
        REFERENCES workflow_terminal_fence_receipts(receipt_id),
    outcome_hash TEXT NOT NULL CHECK(length(outcome_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE tool_catalog_snapshots (
    generation INTEGER PRIMARY KEY AUTOINCREMENT,
    content_fingerprint TEXT NOT NULL UNIQUE CHECK(length(content_fingerprint) = 64),
    specs_json TEXT NOT NULL,
    created_at REAL NOT NULL CHECK(created_at >= 0)
) STRICT;

CREATE TABLE provider_projection_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    invocation_id TEXT NOT NULL REFERENCES provider_invocations(invocation_id) ON DELETE CASCADE,
    invocation_version INTEGER NOT NULL CHECK(invocation_version >= 1),
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    execution_session_id TEXT NOT NULL REFERENCES execution_sessions(session_id),
    request_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(invocation_id, invocation_version)
) STRICT;

CREATE INDEX provider_projection_outbox_cursor_idx
    ON provider_projection_outbox(sequence, execution_session_id);

INSERT INTO execution_users(user_id, created_at) VALUES('harness-system', 0);

ALTER TABLE continuations ADD COLUMN context_stage_id TEXT;
ALTER TABLE continuations ADD COLUMN context_stage_hash TEXT;

CREATE TABLE memory_outbox (
    intent_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    continuation_id TEXT REFERENCES continuations(continuation_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES execution_users(user_id),
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    memory_text TEXT,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    state TEXT NOT NULL CHECK(state IN (
        'pending','claimed','applied','dead_letter','skipped_non_text'
    )),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    claim_owner TEXT,
    claim_token TEXT,
    claim_expires_at REAL,
    retry_at REAL NOT NULL CHECK(retry_at >= 0),
    error_code TEXT,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    settled_at REAL,
    FOREIGN KEY(session_id, user_id)
        REFERENCES execution_sessions(session_id, user_id),
    CHECK(
        (state = 'claimed' AND claim_owner IS NOT NULL AND claim_token IS NOT NULL
            AND claim_expires_at IS NOT NULL)
        OR
        (state <> 'claimed' AND claim_owner IS NULL AND claim_token IS NULL
            AND claim_expires_at IS NULL)
    ),
    CHECK(
        (memory_text IS NULL AND state = 'skipped_non_text')
        OR
        (memory_text IS NOT NULL AND length(trim(memory_text)) > 0
            AND state <> 'skipped_non_text')
    )
) STRICT;

CREATE INDEX memory_outbox_claim_idx
    ON memory_outbox(state, retry_at, created_at, intent_id);
CREATE INDEX memory_outbox_run_idx ON memory_outbox(run_id, source_event_id);

CREATE TABLE agent_identity_bindings (
    session_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    identity_hash TEXT NOT NULL CHECK(length(identity_hash) = 64),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    UNIQUE(deployment_id, household_id, actor_id, session_id)
) STRICT;

CREATE TABLE context_preparation_staging (
    stage_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('root','continuation')),
    identity_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES execution_users(user_id),
    session_id TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
    mode TEXT NOT NULL CHECK(mode IN ('sdk_prepared','consumer_prepared')),
    state TEXT NOT NULL CHECK(state IN ('preparing','staged','consumed','abandoned')),
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at REAL,
    memory_result_id TEXT,
    memory_result_hash TEXT,
    memory_query_hash TEXT,
    memory_write_fence TEXT,
    outcome TEXT CHECK(outcome IS NULL OR outcome IN ('ready','degraded_empty')),
    error_code TEXT,
    product_result_hash TEXT,
    source_snapshot_ref TEXT,
    turn_started_at REAL CHECK(turn_started_at IS NULL OR turn_started_at >= 0),
    private_snapshot BLOB,
    private_snapshot_hash TEXT,
    consumed_run_id TEXT REFERENCES runs(run_id),
    consumed_continuation_id TEXT REFERENCES continuations(continuation_id),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0),
    FOREIGN KEY(session_id, user_id)
        REFERENCES execution_sessions(session_id, user_id),
    CHECK(
        (state = 'preparing' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND private_snapshot IS NULL)
        OR
        (state = 'staged' AND lease_owner IS NULL AND lease_token IS NULL
            AND lease_expires_at IS NULL AND private_snapshot IS NOT NULL
            AND private_snapshot_hash IS NOT NULL)
        OR
        (state IN ('consumed','abandoned') AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL
            AND private_snapshot IS NULL AND private_snapshot_hash IS NOT NULL)
    ),
    CHECK(
        (consumed_run_id IS NULL OR consumed_continuation_id IS NULL)
        AND (state = 'consumed' OR (
            consumed_run_id IS NULL AND consumed_continuation_id IS NULL
        ))
    )
) STRICT;

CREATE INDEX context_preparation_cleanup_idx
    ON context_preparation_staging(state, updated_at, stage_id);

CREATE TABLE memory_recall_releases (
    release_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL UNIQUE
        REFERENCES context_preparation_staging(stage_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    query_hash TEXT NOT NULL CHECK(length(query_hash) = 64),
    result_id TEXT NOT NULL,
    result_hash TEXT NOT NULL CHECK(length(result_hash) = 64),
    write_fence TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','released')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    retry_at REAL NOT NULL CHECK(retry_at >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    released_at REAL
) STRICT;

CREATE INDEX memory_recall_releases_pending_idx
    ON memory_recall_releases(state, retry_at, release_id);

CREATE TABLE committed_turn_outbox (
    intent_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL UNIQUE,
    deployment_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    state TEXT NOT NULL CHECK(state IN ('pending','claimed','delivered','retry_wait','dead_letter')),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    retry_at REAL NOT NULL CHECK(retry_at >= 0),
    error_code TEXT,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    settled_at REAL
) STRICT;

CREATE INDEX committed_turn_outbox_claim_idx
    ON committed_turn_outbox(state, retry_at, created_at, intent_id);
