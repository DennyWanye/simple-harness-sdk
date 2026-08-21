-- SPDX-FileCopyrightText: 2026 DennyWanye
-- SPDX-License-Identifier: Apache-2.0

-- This suffix is composed with the frozen v1 execution and v2 context-authority
-- DDL by schema.fresh_descriptor().  Only the resulting v3 descriptor is ever
-- recorded or accepted; v1/v2 migration histories are deliberately incompatible.

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
