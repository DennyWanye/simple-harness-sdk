# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""DDL for the H1-H admission seams (migration 20).

The operation/action table is deliberately an identity bridge.  It does not copy
the action state, handoff count, reconciliation label, or approval state; those
facts remain in the existing action ledger and are read by the runtime mapper.
"""

DDL = """
CREATE TABLE planning_lane_grants (
 grant_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision >= 1),
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 tenant_id TEXT NOT NULL,
 scope_id TEXT NOT NULL,
 grantee_id TEXT NOT NULL,
 issuer_id TEXT NOT NULL,
 issuer_command_id TEXT NOT NULL,
 issuer_receipt_hash TEXT NOT NULL CHECK(length(issuer_receipt_hash)=64),
 active INTEGER NOT NULL CHECK(active IN (0,1)),
 allowed_decisions_json TEXT NOT NULL,
 policy_hash TEXT NOT NULL CHECK(length(policy_hash)=64),
 not_before_ms INTEGER NOT NULL CHECK(not_before_ms >= 0),
 expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > not_before_ms),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 grant_json TEXT NOT NULL,
 PRIMARY KEY(grant_id, revision),
 UNIQUE(issuer_command_id)
) STRICT;
CREATE INDEX planning_lane_grants_mission_idx
 ON planning_lane_grants(mission_id,scope_id,grantee_id,grant_id,revision);

CREATE TABLE planning_request_authority_bindings (
 request_id TEXT PRIMARY KEY NOT NULL REFERENCES planning_requests(request_id),
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 tenant_id TEXT NOT NULL,
 scope_id TEXT NOT NULL,
 planner_principal_id TEXT NOT NULL,
 grant_id TEXT NOT NULL,
 grant_revision INTEGER NOT NULL,
 grant_hash TEXT NOT NULL CHECK(length(grant_hash)=64),
 binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64),
 binding_json TEXT NOT NULL,
 FOREIGN KEY(grant_id,grant_revision) REFERENCES planning_lane_grants(grant_id,revision)
) STRICT;

CREATE TABLE planning_operation_action_links (
 operation_id TEXT PRIMARY KEY NOT NULL,
 request_hash TEXT NOT NULL,
 operation_occurrence_id TEXT NOT NULL UNIQUE,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 envelope_hash TEXT NOT NULL CHECK(length(envelope_hash)=64),
 principal_id TEXT NOT NULL,
 scope_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 producer_task_id TEXT NOT NULL,
 producer_htn_occurrence_id TEXT NOT NULL,
 producer_contract_revision INTEGER NOT NULL CHECK(producer_contract_revision>=0),
 producer_plan_revision INTEGER NOT NULL CHECK(producer_plan_revision>=0),
 action_key TEXT NOT NULL UNIQUE REFERENCES actions(action_key),
 action_id TEXT NOT NULL,
 action_version INTEGER NOT NULL CHECK(action_version>=1),
 params_hash TEXT NOT NULL CHECK(length(params_hash)=64),
 idempotency_key TEXT NOT NULL,
 provenance_receipt_id TEXT NOT NULL,
 link_hash TEXT NOT NULL CHECK(length(link_hash)=64),
 link_json TEXT NOT NULL,
 FOREIGN KEY(operation_id,request_hash)
   REFERENCES operation_identities(operation_id,request_hash),
 FOREIGN KEY(operation_occurrence_id)
   REFERENCES operation_bindings(operation_occurrence_id)
) STRICT;
CREATE INDEX planning_operation_action_links_mission_idx
 ON planning_operation_action_links(mission_id,operation_id,action_key);

CREATE TABLE planning_admission_checks (
 check_id TEXT PRIMARY KEY NOT NULL,
 request_id TEXT NOT NULL REFERENCES planning_requests(request_id),
 decision_id TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN (
   'PREFLIGHT','PREVIEW','DEFERRED','PRECOMMIT','APPLIED','REJECTED',
   'SOURCE_UNAVAILABLE','NO_STATE_CHANGE'
 )),
 snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
 decision_hash TEXT NOT NULL CHECK(length(decision_hash)=64),
 authority_hash TEXT,
 operations_hash TEXT,
 delta_hash TEXT,
 check_schema TEXT NOT NULL,
 detail_json TEXT NOT NULL,
 checked_at_ms INTEGER NOT NULL CHECK(checked_at_ms>=0)
) STRICT;
CREATE INDEX planning_admission_checks_decision_idx
 ON planning_admission_checks(request_id,decision_id,checked_at_ms);
"""

TABLES = (
    "planning_lane_grants",
    "planning_request_authority_bindings",
    "planning_operation_action_links",
    "planning_admission_checks",
)

__all__ = ("DDL", "TABLES")
