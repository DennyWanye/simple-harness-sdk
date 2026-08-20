-- SPDX-FileCopyrightText: 2026 DennyWanye
-- SPDX-License-Identifier: Apache-2.0

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
