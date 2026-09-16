# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FULL-TARGET P2.3c DDL (migration 17): the accept-side receipts and output index.

Three tables, all additive, all STRICT, all Mission-owned.  Migration 16's bytes are
untouched — a deployed library that already ran 16 applies 17 on top of it.

``acceptance_commit_receipts``
    §17.4 for the accept side.  ``accept_review`` / ``commit_goal_resolution`` produce
    no plan revision, so they cannot live in ``plan_commit_receipts`` — that table's
    own CHECK requires ``new_plan_revision > base_plan_revision``.  P2.3c part 1
    projected the receipt out of the durable event instead and left a note saying a
    table was the right answer; this is that table.  The unique key is
    ``(mission_id, command_id)`` and ``intent_hash`` is carried beside it, so
    "the same command twice is one commit and one receipt" is one keyed read rather
    than a paged scan of the Mission's events, and "two commands wearing one name"
    is a primary-key hit with a different intent.

``delivery_receipts``
    AER §6.1: how far one accepted output actually travelled.  The rows are the
    *only* source a Mission-root resolution may quote — a receipt handed in on a
    command and never recorded here is a claim, not a record.  ``command_id`` +
    ``intent_hash`` make recording one idempotent under the same §17.4 rule as the
    commit receipts.

``acceptance_outputs``
    "which artifact did this Acceptance accept, at which output port".  P2.3b's
    ``AcceptedOutputsIndex`` needs exactly this and nothing in the schema held it, so
    ``HierarchicalDispatch._recorded_outputs`` returned empty and every consumer that
    declared a DATA port sat in ``WAITING_DATA``.  The port is the load-bearing
    column: guessing it from an artifact path is the all-ancestors sweep §24.1
    decision 4 removed, wearing a typed name.
"""

DDL = """
CREATE TABLE acceptance_commit_receipts (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 command_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('acceptance','goal_resolution')),
 subject_id TEXT NOT NULL,
 intent_hash TEXT NOT NULL CHECK(length(intent_hash)=64),
 read_set_hash TEXT NOT NULL CHECK(length(read_set_hash)=64),
 event_id TEXT NOT NULL,
 output_identity_json TEXT NOT NULL,
 detail_json TEXT NOT NULL,
 applied_at REAL NOT NULL,
 PRIMARY KEY(mission_id, command_id)
) STRICT;
CREATE INDEX acceptance_commit_receipts_intent_idx
 ON acceptance_commit_receipts(mission_id, intent_hash);
CREATE INDEX acceptance_commit_receipts_subject_idx
 ON acceptance_commit_receipts(mission_id, kind, subject_id);

CREATE TABLE delivery_receipts (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 command_id TEXT NOT NULL,
 receipt_id TEXT NOT NULL,
 acceptance_id TEXT NOT NULL REFERENCES acceptances(acceptance_id),
 stage TEXT NOT NULL
  CHECK(stage IN ('PERSISTED','ENQUEUED','SENT','CONFIRMED','FAILED')),
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 operation_id TEXT,
 intent_hash TEXT NOT NULL CHECK(length(intent_hash)=64),
 receipt_json TEXT NOT NULL,
 recorded_at REAL NOT NULL,
 PRIMARY KEY(mission_id, command_id)
) STRICT;
CREATE UNIQUE INDEX delivery_receipts_receipt_idx ON delivery_receipts(mission_id, receipt_id);
CREATE INDEX delivery_receipts_intent_idx ON delivery_receipts(mission_id, intent_hash);
CREATE INDEX delivery_receipts_acceptance_idx ON delivery_receipts(mission_id, acceptance_id);

CREATE TABLE acceptance_outputs (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 acceptance_id TEXT NOT NULL REFERENCES acceptances(acceptance_id),
 output_port TEXT NOT NULL,
 artifact_id TEXT NOT NULL,
 producer_occurrence TEXT NOT NULL,
 producer_task_ref TEXT NOT NULL,
 producer_result_id TEXT NOT NULL,
 support_revision INTEGER NOT NULL CHECK(support_revision>=0),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 source_revision TEXT NOT NULL,
 output_json TEXT NOT NULL,
 recorded_at REAL NOT NULL,
 PRIMARY KEY(acceptance_id, output_port, artifact_id)
) STRICT;
CREATE INDEX acceptance_outputs_mission_idx
 ON acceptance_outputs(mission_id, producer_occurrence, output_port);
"""

#: Every table this migration creates, in creation order.  The legacy leak guard and
#: the migration tests read it instead of re-listing the names by hand.
TABLES: tuple[str, ...] = (
    "acceptance_commit_receipts",
    "delivery_receipts",
    "acceptance_outputs",
)

__all__ = ("DDL", "TABLES")
