# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1 DDL (migration 19): the planning-decision protocol's durable identity.

One migration, three tables, copied verbatim from V2 §8.2 and §36:

* ``mission_planning_protocols`` — the *durable* answer to "is this Mission on the
  legacy PlanProposal wire or the planning-decision wire?".  A legacy Mission has
  no row: absent means legacy, and nothing here ever reads the environment to
  guess.  The switch is irreversible for a Mission (§8.2).
* ``planning_requests`` — the §34 request binding, including ``intent_id`` (the
  conflict check's BL-7 addition).  A decision may only point at a request that was
  written first, so ``planning_decisions.request_id`` has a real foreign key.
* ``planning_decisions`` — one row per planner attempt, identified by
  ``(request_id, attempt_ordinal)``.  Replaying the same raw output returns the same
  row; the same ordinal with different bytes is an identity conflict (§35).

All three are STRICT.  The DDL is one migration because the three tables only make
sense together: a library that had a request but nowhere to put its decision, or a
decision without a binding, would be a half-applied protocol.
"""

DDL = """
CREATE TABLE mission_planning_protocols (
    mission_id TEXT PRIMARY KEY NOT NULL,
    protocol_version TEXT NOT NULL,
    package_version INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    created_at REAL NOT NULL
) STRICT;

CREATE TABLE planning_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    mission_id TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    package_version INTEGER NOT NULL,
    package_hash TEXT NOT NULL,
    base_plan_revision INTEGER NOT NULL,
    requirements_revision INTEGER NOT NULL,
    scope_epoch_digest TEXT NOT NULL,
    subject_bindings_hash TEXT NOT NULL,
    visible_refs_digest TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    created_at REAL NOT NULL
) STRICT;

CREATE TABLE planning_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL,
    attempt_ordinal INTEGER NOT NULL,
    raw_output_hash TEXT NOT NULL,
    raw_artifact_ref TEXT,
    canonical_json TEXT,
    canonical_hash TEXT,
    decision_type TEXT,
    status TEXT NOT NULL,
    rejection_codes_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(request_id, attempt_ordinal),
    FOREIGN KEY(request_id) REFERENCES planning_requests(request_id)
) STRICT;
"""

#: Every table this migration creates, in creation order.  Tests iterate it instead
#: of re-listing the names by hand.
TABLES: tuple[str, ...] = (
    "mission_planning_protocols",
    "planning_requests",
    "planning_decisions",
)

__all__ = ("DDL", "TABLES")
