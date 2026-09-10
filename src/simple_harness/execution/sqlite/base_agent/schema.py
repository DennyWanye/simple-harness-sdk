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
 creation_key TEXT NOT NULL,
 config_json TEXT NOT NULL,
 config_hash TEXT NOT NULL CHECK(length(config_hash) = 64),
 control_generation INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,
 lifecycle TEXT NOT NULL DEFAULT 'open' CHECK(lifecycle IN ('open','closing','closed')),
 lifecycle_updated_at REAL,
 UNIQUE(owner_scope, creation_key)
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
 tool_call_ordinal_from INTEGER,
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
CREATE TABLE base_agent_creation_batches_v1 (
 batch_id TEXT PRIMARY KEY,
 owner_scope TEXT NOT NULL,
 batch_key TEXT NOT NULL,
 batch_fingerprint TEXT NOT NULL CHECK(length(batch_fingerprint) = 64),
 agent_ids_json TEXT NOT NULL,
 config_hashes_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','committed')),
 receipt_json TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(owner_scope, batch_key)
) STRICT;
CREATE TABLE base_agent_control_commands_v1 (
 command_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 kind TEXT NOT NULL CHECK(kind IN ('close','cancel_turn')),
 target_turn_id TEXT,
 control_generation INTEGER NOT NULL,
 request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
 receipt_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE INDEX base_agent_control_commands_v1_agent_idx
 ON base_agent_control_commands_v1(agent_id, kind);
CREATE TABLE base_agent_session_journal_v1 (
 record_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 seq INTEGER NOT NULL CHECK(seq >= 1),
 append_id TEXT NOT NULL,
 kind TEXT NOT NULL
  CHECK(kind IN ('instructions','user_input','assistant','tool_result','feedback')),
 turn_id TEXT,
 protocol_group_id TEXT NOT NULL,
 message_json TEXT NOT NULL,
 content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
 provenance TEXT NOT NULL CHECK(provenance IN ('input','model','ledger','derived')),
 visibility TEXT NOT NULL CHECK(visibility IN ('context','journal_only')),
 full_record_seq INTEGER,
 lease_epoch INTEGER NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(agent_id, seq)
) STRICT;
CREATE INDEX base_agent_session_journal_v1_group_idx
 ON base_agent_session_journal_v1(agent_id, protocol_group_id);
CREATE TABLE base_agent_journal_appends_v1 (
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 append_id TEXT NOT NULL,
 append_hash TEXT NOT NULL CHECK(length(append_hash) = 64),
 seq_from INTEGER NOT NULL,
 seq_to INTEGER NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(agent_id, append_id)
) STRICT;
CREATE TABLE base_agent_context_selections_v1 (
 selection_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 turn_id TEXT,
 revision INTEGER NOT NULL,
 source_highwater INTEGER NOT NULL,
 selected_seqs_json TEXT NOT NULL,
 dropped_ranges_json TEXT NOT NULL,
 required_over_budget INTEGER NOT NULL CHECK(required_over_budget IN (0,1)),
 message_tokens INTEGER NOT NULL,
 tool_tokens INTEGER NOT NULL,
 budget_tokens INTEGER NOT NULL,
 policy_hash TEXT NOT NULL CHECK(length(policy_hash) = 64),
 tokenizer_fingerprint TEXT NOT NULL,
 query_hash TEXT,
 index_generation TEXT,
 provider_request_id TEXT UNIQUE,
 request_hash TEXT CHECK(request_hash IS NULL OR length(request_hash) = 64),
 request_tokens INTEGER,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE INDEX base_agent_context_selections_v1_agent_idx
 ON base_agent_context_selections_v1(agent_id, revision);
CREATE TABLE base_agent_session_summaries_v1 (
 summary_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 scope TEXT NOT NULL CHECK(scope IN ('dropped_history')),
 from_seq INTEGER NOT NULL,
 to_seq INTEGER NOT NULL,
 source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
 summary_json TEXT NOT NULL,
 generated_by TEXT NOT NULL,
 validity TEXT NOT NULL CHECK(validity IN ('valid','superseded')),
 created_at REAL NOT NULL,
 UNIQUE(agent_id, from_seq, to_seq, source_hash)
) STRICT;
CREATE TABLE base_agent_session_vectors_v1 (
 vector_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 record_seq INTEGER NOT NULL,
 source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
 embedding_fingerprint TEXT NOT NULL,
 dim INTEGER NOT NULL CHECK(dim >= 1),
 vector BLOB NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(agent_id, record_seq, embedding_fingerprint)
) STRICT;
CREATE INDEX base_agent_session_vectors_v1_agent_idx
 ON base_agent_session_vectors_v1(agent_id, embedding_fingerprint);
CREATE TABLE base_agent_index_jobs_v1 (
 job_id TEXT PRIMARY KEY,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 record_seq INTEGER NOT NULL,
 source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
 embedding_fingerprint TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','claimed','done','error')),
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT,
 lease_expires_at REAL,
 error_code TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 UNIQUE(agent_id, record_seq, embedding_fingerprint)
) STRICT;
CREATE INDEX base_agent_index_jobs_v1_state_idx
 ON base_agent_index_jobs_v1(state, lease_expires_at);
CREATE TABLE base_agent_upgrade_receipt_v1 (
 id INTEGER PRIMARY KEY CHECK(id = 1),
 receipt_json TEXT NOT NULL,
 receipt_hash TEXT NOT NULL CHECK(length(receipt_hash) = 64)
) STRICT;
"""

__all__ = ("DDL",)
