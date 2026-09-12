# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Additive Mission-level system pools; schema aggregation assigns the version."""

DDL = """
CREATE TABLE mission_system_tail_pools (
 pool_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 subject_id TEXT NOT NULL UNIQUE REFERENCES budget_reservations(subject_id),
 purpose TEXT NOT NULL CHECK(purpose IN ('conflict','synthesis')),
 mission_revision TEXT NOT NULL,
 binding_json TEXT NOT NULL,
 request_json TEXT NOT NULL,
 remaining_attempts INTEGER NOT NULL CHECK(remaining_attempts>=0),
 state TEXT NOT NULL CHECK(state IN ('HELD','RELEASED')),
 release_reason TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE TABLE mission_system_tail_tasks (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
 pool_id TEXT NOT NULL REFERENCES mission_system_tail_pools(pool_id),
 hold_id TEXT NOT NULL UNIQUE REFERENCES budget_tail_holds(hold_id),
 request_json TEXT NOT NULL,
 binding_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
"""

__all__ = ("DDL",)
