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
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    ddl: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.ddl.encode("utf-8")).hexdigest()


DDL_V1 = """
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
 unknown INTEGER NOT NULL DEFAULT 0,
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


# Step 4 (D4-15): the Blackboard layers that are stored separately from the claims
# (Verified Knowledge, Summaries), the conflict ledger, a claim subject index and the
# (mission, path, version) artifact lineage guard (step-3 leftover L3-2).
DDL_V2 = """
CREATE TABLE knowledge (
 knowledge_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 claim_id TEXT NOT NULL,
 key TEXT,
 status TEXT NOT NULL,
 version INTEGER NOT NULL,
 source_task TEXT NOT NULL,
 json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE INDEX knowledge_mission_idx ON knowledge(mission_id, status, created_at);

CREATE TABLE summaries (
 summary_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 scope TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 version TEXT NOT NULL,
 json TEXT NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(mission_id, scope, subject_id)
) STRICT;

CREATE TABLE conflicts (
 conflict_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 key TEXT NOT NULL,
 state TEXT NOT NULL,
 task_id TEXT,
 version INTEGER NOT NULL,
 json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE INDEX conflicts_mission_idx ON conflicts(mission_id, state, key);

ALTER TABLE claims ADD COLUMN key TEXT;
CREATE INDEX claims_mission_key_idx ON claims(mission_id, key);

CREATE UNIQUE INDEX artifacts_lineage_idx ON artifacts(mission_id, path, version);
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "orchestrator-step02", DDL_V1),
    Migration(2, "orchestrator-step04", DDL_V2),
)
SCHEMA_VERSION = MIGRATIONS[-1].version
SCHEMA_NAME = MIGRATIONS[-1].name
DDL = DDL_V1  # kept for readers of the step-2/3 descriptor


def checksum() -> str:
    return MIGRATIONS[-1].checksum


__all__ = (
    "DDL",
    "DDL_V1",
    "DDL_V2",
    "MIGRATIONS",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "Migration",
    "checksum",
)
