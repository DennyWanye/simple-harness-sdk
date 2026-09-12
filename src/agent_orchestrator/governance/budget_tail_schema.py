# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Additive P35 tail/money schema; the migration owner assigns its version."""

DDL = """
ALTER TABLE budget_accounts ADD COLUMN reserved_attempts INTEGER NOT NULL DEFAULT 0
 CHECK(reserved_attempts>=0);
ALTER TABLE provider_token_grants ADD COLUMN price_json TEXT;
ALTER TABLE provider_token_grants ADD COLUMN price_digest TEXT;
ALTER TABLE provider_token_grants ADD COLUMN cost_upper_micros INTEGER
 CHECK(cost_upper_micros IS NULL OR cost_upper_micros>=0);
ALTER TABLE provider_token_grants ADD COLUMN actual_cost_micros INTEGER
 CHECK(actual_cost_micros IS NULL OR actual_cost_micros>=0);
CREATE TABLE budget_tail_holds (
 hold_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
 subject_id TEXT NOT NULL UNIQUE REFERENCES budget_reservations(subject_id),
 task_revision TEXT NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN ('selection','critic','conflict','synthesis')),
 request_json TEXT NOT NULL,
 remaining_attempts INTEGER NOT NULL CHECK(remaining_attempts>=0),
 state TEXT NOT NULL CHECK(state IN ('HELD','RELEASED')),
 release_reason TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE TABLE budget_tail_transfers (
 hold_id TEXT NOT NULL REFERENCES budget_tail_holds(hold_id),
 transfer_id TEXT NOT NULL,
 request_json TEXT NOT NULL,
 receipt_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(hold_id,transfer_id)
) STRICT;
"""

__all__ = ("DDL",)
