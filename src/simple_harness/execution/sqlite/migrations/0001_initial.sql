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
    state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'acked')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    claimed_by TEXT,
    created_at REAL NOT NULL CHECK(created_at >= 0),
    claimed_at REAL,
    acked_at REAL,
    UNIQUE(run_id, fifo_seq),
    CHECK((state = 'pending' AND claimed_by IS NULL AND claimed_at IS NULL AND acked_at IS NULL)
       OR (state = 'claimed' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL AND acked_at IS NULL)
       OR (state = 'acked' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL AND acked_at IS NOT NULL))
) STRICT;

CREATE INDEX continuations_fifo_idx ON continuations(run_id, state, fifo_seq);

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

CREATE TABLE child_signals (
    signal_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    child_run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'acked')),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    created_at REAL NOT NULL CHECK(created_at >= 0),
    updated_at REAL NOT NULL CHECK(updated_at >= 0)
) STRICT;

CREATE INDEX child_signals_parent_state_idx ON child_signals(parent_run_id, state, created_at);

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

CREATE TABLE provider_invocations (
    invocation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    state TEXT NOT NULL CHECK(state IN ('claimed', 'handed_off', 'succeeded', 'failed', 'unknown')),
    response_json TEXT,
    usage_json TEXT,
    error_code TEXT,
    claimed_at REAL NOT NULL CHECK(claimed_at >= 0),
    handed_off_at REAL,
    settled_at REAL,
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    UNIQUE(run_id, request_id, invocation_id)
) STRICT;

CREATE INDEX provider_invocations_run_state_idx ON provider_invocations(run_id, state);

CREATE TABLE run_fences (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch >= 1),
    state TEXT NOT NULL CHECK(state IN ('active', 'released', 'cancelled')),
    acquired_at REAL NOT NULL CHECK(acquired_at >= 0),
    released_at REAL
) STRICT;

CREATE TABLE execution_effects (
    effect_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
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
    UNIQUE(run_id, call_id, effect_id)
) STRICT;

CREATE INDEX execution_effects_run_state_idx ON execution_effects(run_id, state);

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

