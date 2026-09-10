# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Execution schema v10 additions: durable BaseAgent bindings, turns, results, delegations.

The four tables are appended to the frozen v9 descriptor to form the v10 fresh
descriptor.  Later BaseAgent slices append to this DDL inside the same v10 and
recompute the descriptor; no second incompatible v10 may exist.
"""

DDL = """
CREATE TABLE base_agent_bindings_v1 (
 agent_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 owner_scope TEXT NOT NULL,
 api_mode TEXT NOT NULL CHECK(api_mode = 'base_agent_v1'),
 role TEXT NOT NULL CHECK(role IN ('root','child')),
 creation_key TEXT NOT NULL UNIQUE,
 config_json TEXT NOT NULL,
 config_hash TEXT NOT NULL CHECK(length(config_hash) = 64),
 control_generation INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE base_agent_turns_v1 (
 turn_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 input_id TEXT NOT NULL,
 input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
 input_json TEXT NOT NULL,
 continuation_id TEXT,
 seq INTEGER NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('queued','running','result_pending','committed','failed')),
 staged_result_hash TEXT CHECK(staged_result_hash IS NULL OR length(staged_result_hash) = 64),
 staged_result_json TEXT,
 provider_turn_ordinal_from INTEGER,
 provider_turn_ordinal_to INTEGER,
 lease_epoch INTEGER,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(agent_id, input_id),
 UNIQUE(agent_id, seq)
) STRICT;
CREATE TABLE base_agent_turn_results_v1 (
 turn_id TEXT PRIMARY KEY REFERENCES base_agent_turns_v1(turn_id),
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 result_hash TEXT NOT NULL CHECK(length(result_hash) = 64),
 result_json TEXT NOT NULL,
 commit_receipt_id TEXT NOT NULL UNIQUE,
 usage_refs_json TEXT,
 committed_at REAL NOT NULL
) STRICT;
CREATE INDEX base_agent_turn_results_v1_agent_idx ON base_agent_turn_results_v1(agent_id);
CREATE TABLE base_agent_delegations_v1 (
 delegation_id TEXT PRIMARY KEY,
 parent_agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 parent_turn_id TEXT NOT NULL REFERENCES base_agent_turns_v1(turn_id),
 ordinal INTEGER NOT NULL,
 child_agent_id TEXT NOT NULL UNIQUE,
 child_run_id TEXT NOT NULL UNIQUE,
 ticket_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('reserved','launched','settled','failed')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(parent_turn_id, ordinal)
) STRICT;
"""

__all__ = ("DDL",)
