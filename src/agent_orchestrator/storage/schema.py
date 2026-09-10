# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator library DDL (``orchestrator.db``): Event Store + Current State Store (§16.3).

One frozen descriptor per package minor version.  Entities are stored as canonical
JSON documents next to the columns the orchestrator queries or guards (status,
version, lease, identity keys); the JSON is the record of truth for the entity,
the columns are indexes.  Never opened by the SDK; never shares a file with
``execution.db`` (plan D2).
"""

from __future__ import annotations

import hashlib

SCHEMA_VERSION = 1
SCHEMA_NAME = "orchestrator-step02"

DDL = """
CREATE TABLE orch_schema_migrations (
 version INTEGER PRIMARY KEY,
 name TEXT NOT NULL,
 checksum TEXT NOT NULL,
 applied_at REAL NOT NULL
) STRICT;

CREATE TABLE events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE,
 idempotency_key TEXT NOT NULL UNIQUE,
 type TEXT NOT NULL,
 trace_id TEXT NOT NULL,
 mission_id TEXT NOT NULL,
 task_id TEXT,
 attempt_id TEXT,
 actor_type TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 schema_version INTEGER NOT NULL
) STRICT;
CREATE INDEX events_mission_idx ON events(mission_id, seq);

CREATE TABLE missions (
 mission_id TEXT PRIMARY KEY,
 tenant_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,
 status TEXT NOT NULL,
 version INTEGER NOT NULL,
 spec_hash TEXT NOT NULL,
 json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(tenant_id, idempotency_key)
) STRICT;

CREATE TABLE tasks (
 task_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 ordinal INTEGER NOT NULL,
 status TEXT NOT NULL,
 version INTEGER NOT NULL,
 json TEXT NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(mission_id, ordinal)
) STRICT;

CREATE TABLE attempts (
 attempt_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 ordinal INTEGER NOT NULL,
 status TEXT NOT NULL,
 version INTEGER NOT NULL,
 lease_owner TEXT,
 lease_expires_at REAL,
 agent_id TEXT,
 turn_id TEXT,
 json TEXT NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(task_id, ordinal)
) STRICT;
CREATE INDEX attempts_status_idx ON attempts(status, lease_expires_at);

CREATE TABLE dispatch_intents (
 intent_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL,
 subject_id TEXT NOT NULL UNIQUE,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 state TEXT NOT NULL,
 version INTEGER NOT NULL,
 creation_key TEXT NOT NULL UNIQUE,
 input_id TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 config_json TEXT NOT NULL,
 expected_turn_id TEXT,
 agent_id TEXT,
 receipt_json TEXT,
 lease_owner TEXT,
 lease_expires_at REAL,
 replays INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE INDEX dispatch_intents_state_idx ON dispatch_intents(state, created_at);

CREATE TABLE results (
 result_id TEXT PRIMARY KEY,
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 turn_id TEXT NOT NULL,
 result_hash TEXT NOT NULL,
 verification_state TEXT NOT NULL,
 verdict TEXT,
 json TEXT NOT NULL,
 received_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(attempt_id, turn_id)
) STRICT;
CREATE INDEX results_verification_idx ON results(verification_state, received_at);

CREATE TABLE verifications (
 verification_id TEXT PRIMARY KEY,
 result_id TEXT NOT NULL REFERENCES results(result_id),
 attempt_id TEXT NOT NULL,
 layer TEXT NOT NULL,
 status TEXT NOT NULL,
 detail_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(result_id, layer)
) STRICT;

CREATE TABLE claims (
 claim_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 result_id TEXT NOT NULL REFERENCES results(result_id),
 status TEXT NOT NULL,
 version INTEGER NOT NULL,
 json TEXT NOT NULL,
 updated_at REAL NOT NULL
) STRICT;

CREATE TABLE artifacts (
 artifact_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 task_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 path TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 version INTEGER NOT NULL,
 json TEXT NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(attempt_id, path, version)
) STRICT;

CREATE TABLE budget_accounts (
 account_id TEXT PRIMARY KEY,
 scope TEXT NOT NULL,
 parent_id TEXT,
 mission_id TEXT NOT NULL,
 limits_json TEXT NOT NULL,
 reserved_tokens INTEGER NOT NULL DEFAULT 0,
 settled_tokens INTEGER NOT NULL DEFAULT 0,
 reserved_cost_micros INTEGER NOT NULL DEFAULT 0,
 settled_cost_micros INTEGER NOT NULL DEFAULT 0,
 unpriced_settlements INTEGER NOT NULL DEFAULT 0,
 attempts_created INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL,
 updated_at REAL NOT NULL
) STRICT;

CREATE TABLE budget_reservations (
 reservation_id TEXT PRIMARY KEY,
 account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
 mission_id TEXT NOT NULL,
 subject_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL,
 reserved_tokens INTEGER NOT NULL,
 reserved_cost_micros INTEGER NOT NULL,
 settled_tokens INTEGER,
 settled_cost_micros INTEGER,
 unpriced INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;

CREATE TABLE imported_usage (
 usage_ref TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL,
 mission_id TEXT NOT NULL,
 input_tokens INTEGER NOT NULL,
 output_tokens INTEGER NOT NULL,
 cost_micros INTEGER,
 unpriced INTEGER NOT NULL,
 imported_at REAL NOT NULL
) STRICT;

CREATE TABLE commit_receipts (
 commit_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 base_version INTEGER,
 proposal_hash TEXT NOT NULL,
 receipt_json TEXT NOT NULL,
 applied_at REAL NOT NULL
) STRICT;
"""


def checksum() -> str:
    return hashlib.sha256(DDL.encode("utf-8")).hexdigest()


__all__ = ("DDL", "SCHEMA_NAME", "SCHEMA_VERSION", "checksum")
