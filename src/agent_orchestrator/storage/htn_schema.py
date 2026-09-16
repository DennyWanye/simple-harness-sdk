# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FULL-TARGET P1.2 DDL: obligations, HTN plan structure, acceptance, evidence, operations.

One additive migration (schema aggregation assigns the version).  Every table is
STRICT and carries its Mission owner, except the three registries whose subject is
deliberately cross-Mission and says so in its comment:

* ``method_contracts`` — a method definition is registry knowledge, not Mission
  state; ``(method_id, method_version)`` is immutable and ``trial_scope_mission``
  names the single Mission a TRIAL_ADMITTED method may be used in (§7.3).
* ``input_manifests`` — an immutable resolved input set, addressed by
  ``manifest_hash``.  The content row is deliberately Mission-free so that reusing
  an identical input set across Missions is one explicit read of one immutable
  artefact; *who* uses it is the Mission-owned ``input_manifest_bindings``, and a
  second consumer adds a binding row rather than rewriting the content.
  ``origin_mission_id`` records which Mission first froze it, for provenance only.
* ``operation_identities`` — one frozen ``request_hash`` per ``OperationId``
  (AER §18.2); a binding that quotes a different hash fails the foreign key,
  which is the storage-level form of ``OPERATION_PAYLOAD_CONFLICT``.

These tables are written only by the new full-target path.  No legacy reader or
writer touches them: an upgraded legacy library keeps every one of them empty.
"""

DDL = """
CREATE TABLE task_semantics (
 task_id TEXT NOT NULL,
 binding_revision INTEGER NOT NULL CHECK(binding_revision>=0),
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 obligation_id TEXT NOT NULL,
 form TEXT NOT NULL CHECK(form IN ('compound','primitive')),
 semantic_scope TEXT NOT NULL,
 goal_signature_id TEXT NOT NULL,
 operator_ref TEXT,
 adopted_method_instance_id TEXT,
 input_binding_revision INTEGER NOT NULL CHECK(input_binding_revision>=0),
 dispatch_generation INTEGER NOT NULL CHECK(dispatch_generation>=0),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 binding_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(task_id, binding_revision)
) STRICT;
CREATE INDEX task_semantics_mission_idx ON task_semantics(mission_id, form, task_id);
CREATE INDEX task_semantics_obligation_idx ON task_semantics(mission_id, obligation_id);

CREATE TABLE method_contracts (
 method_id TEXT NOT NULL,
 method_version INTEGER NOT NULL CHECK(method_version>=1),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 registry_status TEXT NOT NULL CHECK(registry_status IN
  ('DRAFT','STRUCTURALLY_VALID','TRIAL_ADMITTED','EVALUATED','ADMITTED','SUSPENDED',
   'REJECTED','RETIRED')),
 author TEXT NOT NULL CHECK(author IN ('system','model')),
 trial_scope_mission TEXT,
 registration_json TEXT NOT NULL,
 contract_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(method_id, method_version),
 CHECK(registry_status<>'TRIAL_ADMITTED' OR trial_scope_mission IS NOT NULL),
 CHECK(author<>'model' OR registry_status='DRAFT')
) STRICT;
CREATE UNIQUE INDEX method_contracts_hash_idx ON method_contracts(content_hash);
CREATE INDEX method_contracts_status_idx ON method_contracts(registry_status, method_id);

CREATE TABLE method_instances (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 instance_id TEXT NOT NULL,
 goal_task_id TEXT NOT NULL,
 goal_occurrence_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 method_id TEXT NOT NULL,
 method_version INTEGER NOT NULL CHECK(method_version>=1),
 method_content_hash TEXT NOT NULL,
 plan_revision INTEGER NOT NULL CHECK(plan_revision>=0),
 state TEXT NOT NULL CHECK(state IN ('DRAFT','ADOPTED','RETIRED')),
 parameters_digest TEXT NOT NULL,
 draft_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(mission_id, instance_id)
) STRICT;
CREATE INDEX method_instances_goal_idx
 ON method_instances(mission_id, goal_task_id, plan_revision);
CREATE INDEX method_instances_occurrence_idx
 ON method_instances(mission_id, goal_occurrence_id, state);
CREATE INDEX method_instances_obligation_idx ON method_instances(mission_id, obligation_id);

CREATE TABLE method_child_occurrences (
 instance_id TEXT NOT NULL,
 slot_key TEXT NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 occurrence_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 goal_occurrence_id TEXT NOT NULL,
 requiredness TEXT NOT NULL CHECK(requiredness IN
  ('required','optional_authorized','conditional')),
 reuse_policy TEXT NOT NULL CHECK(reuse_policy IN
  ('new_work','reuse_accepted','share_active')),
 binding_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(instance_id, slot_key),
 FOREIGN KEY(mission_id, instance_id) REFERENCES method_instances(mission_id, instance_id)
) STRICT;
CREATE INDEX method_child_occurrences_occurrence_idx
 ON method_child_occurrences(mission_id, occurrence_id);
CREATE INDEX method_child_occurrences_shared_idx
 ON method_child_occurrences(mission_id, goal_occurrence_id, reuse_policy);
CREATE INDEX method_child_occurrences_obligation_idx
 ON method_child_occurrences(mission_id, obligation_id);

CREATE TABLE plan_revisions (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 revision INTEGER NOT NULL CHECK(revision>=0),
 state TEXT NOT NULL CHECK(state IN ('PREPARED','ACTIVE','RETIRED')),
 base_revision INTEGER CHECK(base_revision IS NULL OR base_revision>=0),
 snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
 delta_id TEXT,
 read_set_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(mission_id, revision),
 CHECK(base_revision IS NULL OR base_revision<revision)
) STRICT;
CREATE UNIQUE INDEX plan_revisions_active_idx ON plan_revisions(mission_id)
 WHERE state = 'ACTIVE';
CREATE INDEX plan_revisions_state_idx ON plan_revisions(mission_id, state, revision);

CREATE TABLE plan_memberships (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 revision INTEGER NOT NULL,
 occurrence_id TEXT NOT NULL,
 task_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 instance_id TEXT,
 form TEXT NOT NULL CHECK(form IN ('compound','primitive')),
 requiredness TEXT NOT NULL CHECK(requiredness IN
  ('required','optional_authorized','conditional')),
 adopted INTEGER NOT NULL CHECK(adopted IN (0,1)),
 member_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, revision, occurrence_id),
 FOREIGN KEY(mission_id, revision) REFERENCES plan_revisions(mission_id, revision)
) STRICT;
CREATE INDEX plan_memberships_task_idx ON plan_memberships(mission_id, task_id, revision);
CREATE INDEX plan_memberships_obligation_idx
 ON plan_memberships(mission_id, obligation_id, adopted);

CREATE TABLE order_constraints (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 plan_revision INTEGER NOT NULL,
 before_occurrence TEXT NOT NULL,
 after_occurrence TEXT NOT NULL,
 release_condition TEXT NOT NULL CHECK(release_condition IN ('accepted','settled_terminal')),
 constraint_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, plan_revision, before_occurrence, after_occurrence),
 CHECK(before_occurrence<>after_occurrence),
 FOREIGN KEY(mission_id, plan_revision) REFERENCES plan_revisions(mission_id, revision)
) STRICT;
CREATE INDEX order_constraints_before_idx
 ON order_constraints(mission_id, plan_revision, before_occurrence);
CREATE INDEX order_constraints_after_idx
 ON order_constraints(mission_id, plan_revision, after_occurrence);

CREATE TABLE data_requirements (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 plan_revision INTEGER NOT NULL,
 requirement_id TEXT NOT NULL,
 producer_occurrence TEXT NOT NULL,
 output_port TEXT NOT NULL,
 consumer_occurrence TEXT NOT NULL,
 input_port TEXT NOT NULL,
 source_revision_policy TEXT NOT NULL CHECK(source_revision_policy IN
  ('PINNED','FOLLOW_AUTHORIZED_REVISION')),
 requirement_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, plan_revision, requirement_id),
 FOREIGN KEY(mission_id, plan_revision) REFERENCES plan_revisions(mission_id, revision)
) STRICT;
CREATE INDEX data_requirements_consumer_idx
 ON data_requirements(mission_id, plan_revision, consumer_occurrence, input_port);
CREATE INDEX data_requirements_producer_idx
 ON data_requirements(mission_id, plan_revision, producer_occurrence, output_port);

CREATE TABLE bound_inputs (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 plan_revision INTEGER NOT NULL,
 requirement_id TEXT NOT NULL,
 input_binding_revision INTEGER NOT NULL CHECK(input_binding_revision>=0),
 producer_result_id TEXT NOT NULL,
 acceptance_id TEXT NOT NULL,
 artifact_id TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 source_revision TEXT NOT NULL,
 binding_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, plan_revision, requirement_id, input_binding_revision),
 FOREIGN KEY(mission_id, plan_revision, requirement_id)
  REFERENCES data_requirements(mission_id, plan_revision, requirement_id)
) STRICT;
CREATE INDEX bound_inputs_acceptance_idx ON bound_inputs(mission_id, acceptance_id);
CREATE INDEX bound_inputs_artifact_idx ON bound_inputs(mission_id, artifact_id, content_hash);

CREATE TABLE input_manifests (
 manifest_hash TEXT PRIMARY KEY CHECK(length(manifest_hash)=64),
 origin_mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 manifest_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;

CREATE TABLE input_manifest_bindings (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 task_id TEXT NOT NULL,
 manifest_hash TEXT NOT NULL REFERENCES input_manifests(manifest_hash),
 attempt_id TEXT,
 request_id TEXT,
 input_binding_revision INTEGER NOT NULL CHECK(input_binding_revision>=0),
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, task_id, manifest_hash)
) STRICT;
CREATE INDEX input_manifest_bindings_task_idx
 ON input_manifest_bindings(mission_id, task_id, input_binding_revision);
CREATE INDEX input_manifest_bindings_hash_idx
 ON input_manifest_bindings(manifest_hash, mission_id);
CREATE UNIQUE INDEX input_manifest_bindings_request_idx
 ON input_manifest_bindings(mission_id, task_id, request_id) WHERE request_id IS NOT NULL;

CREATE TABLE obligations (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 obligation_id TEXT NOT NULL,
 goal_signature_id TEXT NOT NULL,
 scope TEXT NOT NULL,
 requiredness TEXT NOT NULL CHECK(requiredness IN
  ('required','optional_authorized','conditional')),
 lifecycle TEXT NOT NULL CHECK(lifecycle IN
  ('UNSATISFIED','SATISFIED','CANCELLED','SUPERSEDED')),
 resolution_ref TEXT,
 parent_obligation_id TEXT,
 budget_lineage_ref TEXT,
 failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count>=0),
 spent_tokens INTEGER NOT NULL DEFAULT 0 CHECK(spent_tokens>=0),
 spent_cost_micros INTEGER NOT NULL DEFAULT 0 CHECK(spent_cost_micros>=0),
 spent_attempts INTEGER NOT NULL DEFAULT 0 CHECK(spent_attempts>=0),
 fuel_limit INTEGER NOT NULL CHECK(fuel_limit>=0),
 fuel_used INTEGER NOT NULL DEFAULT 0 CHECK(fuel_used>=0),
 fuel_remaining INTEGER NOT NULL CHECK(fuel_remaining>=0),
 demand_admitted INTEGER NOT NULL DEFAULT 0 CHECK(demand_admitted IN (0,1)),
 obligation_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(mission_id, obligation_id),
 CHECK(fuel_used<=fuel_limit),
 CHECK(demand_admitted=0 OR lifecycle='UNSATISFIED'),
 CHECK(fuel_remaining=fuel_limit-fuel_used),
 CHECK(obligation_id<>parent_obligation_id),
 CHECK(lifecycle<>'SATISFIED' OR resolution_ref IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX obligations_identity_idx ON obligations(obligation_id);
CREATE INDEX obligations_lifecycle_idx ON obligations(mission_id, lifecycle, obligation_id);
CREATE INDEX obligations_funding_idx
 ON obligations(mission_id, goal_signature_id, budget_lineage_ref);

CREATE TABLE obligation_relations (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 parent_obligation_id TEXT NOT NULL,
 child_obligation_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('refines_parent','independent_authorized')),
 active_revision INTEGER NOT NULL CHECK(active_revision>=0),
 detail_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, parent_obligation_id, child_obligation_id, kind),
 CHECK(parent_obligation_id<>child_obligation_id),
 FOREIGN KEY(mission_id, parent_obligation_id) REFERENCES obligations(mission_id, obligation_id),
 FOREIGN KEY(mission_id, child_obligation_id) REFERENCES obligations(mission_id, obligation_id)
) STRICT;
CREATE INDEX obligation_relations_child_idx
 ON obligation_relations(mission_id, child_obligation_id, kind);

CREATE TABLE obligation_expansions (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 obligation_id TEXT NOT NULL,
 method_id TEXT NOT NULL,
 parameters_digest TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=1),
 task_id TEXT,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, obligation_id, method_id, parameters_digest),
 FOREIGN KEY(mission_id, obligation_id) REFERENCES obligations(mission_id, obligation_id)
) STRICT;
CREATE UNIQUE INDEX obligation_expansions_ordinal_idx
 ON obligation_expansions(mission_id, obligation_id, ordinal);

CREATE TABLE obligation_shape_changes (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 obligation_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=1),
 change TEXT NOT NULL CHECK(change IN
  ('task_renamed','method_switched','agent_reassigned','parameters_rebound','successor_task')),
 detail TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, obligation_id, ordinal),
 FOREIGN KEY(mission_id, obligation_id) REFERENCES obligations(mission_id, obligation_id)
) STRICT;

CREATE TABLE requirements_revisions (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 revision INTEGER NOT NULL CHECK(revision>=0),
 revision_id TEXT NOT NULL UNIQUE,
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 authority_subject TEXT,
 revision_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, revision)
) STRICT;

CREATE TABLE review_packages (
 package_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 purpose TEXT NOT NULL CHECK(purpose IN
  ('TASK_CONTENT','METHOD_PLAN','COMPOSITION','ACTION_PROPOSAL','OPERATION_OUTCOME',
   'MISSION_FINAL')),
 review_account TEXT NOT NULL CHECK(review_account IN
  ('task','mission_planning','parent_compound_task','operation_task','mission')),
 obligation_id TEXT NOT NULL,
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 requirements_revision INTEGER NOT NULL CHECK(requirements_revision>=0),
 input_manifest_hash TEXT NOT NULL,
 method_instance_id TEXT,
 package_hash TEXT NOT NULL CHECK(length(package_hash)=64),
 package_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE INDEX review_packages_mission_idx ON review_packages(mission_id, purpose, created_at);
CREATE INDEX review_packages_subject_idx
 ON review_packages(mission_id, subject_kind, subject_id);

CREATE TABLE review_records (
 record_id TEXT PRIMARY KEY,
 package_id TEXT NOT NULL REFERENCES review_packages(package_id),
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 purpose TEXT NOT NULL,
 reviewer_agent_id TEXT NOT NULL,
 reviewer_turn_id TEXT NOT NULL,
 verdict TEXT NOT NULL CHECK(verdict IN ('ACCEPT','REWORK','INCONCLUSIVE','REJECTED')),
 evidence_manifest_hash TEXT NOT NULL,
 official INTEGER NOT NULL CHECK(official IN (0,1)),
 record_hash TEXT NOT NULL CHECK(length(record_hash)=64),
 record_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX review_records_official_idx ON review_records(package_id)
 WHERE official = 1;
CREATE INDEX review_records_package_idx ON review_records(package_id, created_at);
CREATE INDEX review_records_reviewer_idx ON review_records(mission_id, reviewer_agent_id);

CREATE TABLE criterion_evaluations (
 review_id TEXT NOT NULL REFERENCES review_records(record_id),
 criterion_id TEXT NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 package_id TEXT NOT NULL REFERENCES review_packages(package_id),
 verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL','UNKNOWN')),
 check_execution TEXT NOT NULL CHECK(check_execution IN
  ('NOT_RUN','RUNNING','SUCCEEDED','ERROR','CANCELLED')),
 check_receipt_hash TEXT NOT NULL CHECK(length(check_receipt_hash)=64),
 outcome_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(review_id, criterion_id),
 CHECK(verdict<>'PASS' OR check_execution NOT IN ('NOT_RUN','ERROR','CANCELLED'))
) STRICT;
CREATE INDEX criterion_evaluations_criterion_idx
 ON criterion_evaluations(mission_id, criterion_id, verdict);

CREATE TABLE acceptances (
 acceptance_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 task_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 review_record_id TEXT NOT NULL REFERENCES review_records(record_id),
 requirements_revision INTEGER NOT NULL CHECK(requirements_revision>=0),
 contract_revision INTEGER NOT NULL CHECK(contract_revision>=0),
 input_manifest_hash TEXT NOT NULL,
 validity TEXT NOT NULL CHECK(validity IN ('CURRENT','STALE','REVOKED')),
 accepted_at_ms INTEGER NOT NULL CHECK(accepted_at_ms>=0),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 acceptance_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX acceptances_review_idx ON acceptances(review_record_id);
CREATE INDEX acceptances_obligation_idx ON acceptances(mission_id, obligation_id, validity);
CREATE INDEX acceptances_task_idx ON acceptances(mission_id, task_id, requirements_revision);

CREATE TABLE goal_resolutions (
 resolution_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 obligation_id TEXT NOT NULL,
 goal_task_id TEXT NOT NULL,
 requirements_version INTEGER NOT NULL CHECK(requirements_version>=0),
 contract_revision INTEGER NOT NULL CHECK(contract_revision>=0),
 method_instance_id TEXT,
 review_receipt_id TEXT NOT NULL,
 verdict TEXT NOT NULL CHECK(verdict IN ('ACCEPT','REWORK','INCONCLUSIVE','REJECTED')),
 validity TEXT NOT NULL CHECK(validity IN ('CURRENT','STALE','REVOKED')),
 adopted INTEGER NOT NULL CHECK(adopted IN (0,1)),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 resolution_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX goal_resolutions_adopted_idx ON goal_resolutions(mission_id, obligation_id)
 WHERE adopted = 1;
CREATE INDEX goal_resolutions_obligation_idx
 ON goal_resolutions(mission_id, obligation_id, validity);
CREATE INDEX goal_resolutions_task_idx ON goal_resolutions(mission_id, goal_task_id);

CREATE TABLE validity_witnesses (
 witness_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 consumer_kind TEXT NOT NULL,
 consumer_id TEXT NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN
  ('PLAN','START','MAINTAIN','ACCEPT','CONTEXT','DISCLOSE','RECOVERY')),
 scope_id TEXT NOT NULL,
 scope_epoch INTEGER NOT NULL CHECK(scope_epoch>=0),
 support_revision INTEGER NOT NULL CHECK(support_revision>=0),
 truth TEXT NOT NULL CHECK(truth IN ('TRUE','FALSE','UNKNOWN','CONFLICT')),
 freshness TEXT NOT NULL CHECK(freshness IN ('CURRENT','STALE','REVOKED')),
 availability TEXT NOT NULL CHECK(availability IN ('READABLE','REDACTED','UNAVAILABLE')),
 decision TEXT NOT NULL CHECK(decision IN ('USABLE','NEEDS_REVIEW','BLOCKED','UNAVAILABLE')),
 as_of_ms INTEGER NOT NULL CHECK(as_of_ms>=0),
 not_after_ms INTEGER,
 witness_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 CHECK(decision<>'USABLE' OR truth='TRUE')
) STRICT;
CREATE UNIQUE INDEX validity_witnesses_consumer_idx ON validity_witnesses(
 mission_id, consumer_kind, consumer_id, purpose, scope_id, scope_epoch, support_revision);
CREATE INDEX validity_witnesses_scope_idx
 ON validity_witnesses(mission_id, scope_id, scope_epoch, decision);

CREATE TABLE observations (
 observation_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 proposition_key TEXT NOT NULL,
 polarity INTEGER NOT NULL CHECK(polarity IN (0,1)),
 scope_id TEXT NOT NULL,
 source_kind TEXT NOT NULL,
 source_id TEXT NOT NULL,
 coverage TEXT NOT NULL CHECK(coverage IN
  ('AUTHORITATIVE_WITH_SCOPE','BEST_EFFORT','UNKNOWN')),
 observer_id TEXT,
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0),
 query_watermark_ms INTEGER,
 valid_until_ms INTEGER,
 observation_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE INDEX observations_proposition_idx
 ON observations(mission_id, proposition_key, observed_at_ms);
CREATE INDEX observations_scope_idx ON observations(mission_id, scope_id, recorded_at_ms);

CREATE TABLE justification_sets (
 set_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 member_revision INTEGER NOT NULL CHECK(member_revision>=0),
 member_digest TEXT NOT NULL CHECK(length(member_digest)=64),
 rule_ref TEXT,
 detail_json TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX justification_sets_digest_idx
 ON justification_sets(mission_id, subject_kind, subject_id, member_digest);
CREATE INDEX justification_sets_subject_idx
 ON justification_sets(mission_id, subject_kind, subject_id, member_revision);

CREATE TABLE support_members (
 set_id TEXT NOT NULL REFERENCES justification_sets(set_id),
 member_kind TEXT NOT NULL,
 member_id TEXT NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 polarity INTEGER NOT NULL CHECK(polarity IN (0,1)),
 member_revision TEXT,
 member_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(set_id, member_kind, member_id)
) STRICT;
CREATE INDEX support_members_reverse_idx
 ON support_members(mission_id, member_kind, member_id);

CREATE TABLE validity_epochs (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 scope_id TEXT NOT NULL,
 epoch INTEGER NOT NULL CHECK(epoch>=0),
 bumped_by TEXT NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(mission_id, scope_id)
) STRICT;

CREATE TABLE validity_dirty (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 epoch INTEGER NOT NULL CHECK(epoch>=0),
 scope_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('PENDING','RECHECKING','CLEARED')),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(mission_id, subject_kind, subject_id, epoch)
) STRICT;
CREATE INDEX validity_dirty_state_idx ON validity_dirty(mission_id, state, created_at);
CREATE INDEX validity_dirty_scope_idx ON validity_dirty(mission_id, scope_id, state);

CREATE TABLE operation_identities (
 operation_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 request_hash TEXT NOT NULL,
 envelope_hash TEXT NOT NULL CHECK(length(envelope_hash)=64),
 envelope_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 UNIQUE(operation_id, request_hash)
) STRICT;

CREATE TABLE operation_bindings (
 principal_id TEXT NOT NULL,
 scope_id TEXT NOT NULL,
 operation_occurrence_id TEXT NOT NULL,
 operation_id TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 obligation_id TEXT NOT NULL,
 connector_id TEXT NOT NULL,
 connector_version TEXT NOT NULL,
 operation_kind TEXT NOT NULL CHECK(operation_kind IN
  ('READ','STATE_WRITE','EVENT_WRITE','COMPENSATION')),
 envelope_hash TEXT NOT NULL,
 envelope_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(principal_id, scope_id, operation_occurrence_id),
 FOREIGN KEY(operation_id, request_hash)
  REFERENCES operation_identities(operation_id, request_hash)
) STRICT;
CREATE UNIQUE INDEX operation_bindings_occurrence_idx
 ON operation_bindings(operation_occurrence_id);
CREATE INDEX operation_bindings_mission_idx ON operation_bindings(mission_id, operation_id);

CREATE TABLE plan_read_sets (
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 proposal_id TEXT NOT NULL,
 subject_type TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 semantic_revision INTEGER NOT NULL,
 content_hash TEXT,
 item_json TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(mission_id, proposal_id, subject_type, subject_id)
) STRICT;
CREATE INDEX plan_read_sets_subject_idx
 ON plan_read_sets(mission_id, subject_type, subject_id, semantic_revision);

CREATE TABLE plan_commit_receipts (
 command_id TEXT PRIMARY KEY,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 delta_id TEXT NOT NULL,
 base_plan_revision INTEGER NOT NULL CHECK(base_plan_revision>=0),
 new_plan_revision INTEGER NOT NULL CHECK(new_plan_revision>=0),
 intent_hash TEXT NOT NULL CHECK(length(intent_hash)=64),
 read_set_hash TEXT NOT NULL CHECK(length(read_set_hash)=64),
 read_set_json TEXT NOT NULL,
 output_identity_json TEXT NOT NULL,
 receipt_json TEXT NOT NULL,
 applied_at REAL NOT NULL,
 CHECK(new_plan_revision>base_plan_revision)
) STRICT;
CREATE UNIQUE INDEX plan_commit_receipts_revision_idx
 ON plan_commit_receipts(mission_id, new_plan_revision);
CREATE INDEX plan_commit_receipts_intent_idx ON plan_commit_receipts(mission_id, intent_hash);
"""

#: Every table this migration creates, in creation order.  Tests and the legacy
#: guard read it instead of re-listing the names by hand.
TABLES: tuple[str, ...] = (
    "task_semantics",
    "method_contracts",
    "method_instances",
    "method_child_occurrences",
    "plan_revisions",
    "plan_memberships",
    "order_constraints",
    "data_requirements",
    "bound_inputs",
    "input_manifests",
    "input_manifest_bindings",
    "obligations",
    "obligation_relations",
    "obligation_expansions",
    "obligation_shape_changes",
    "requirements_revisions",
    "review_packages",
    "review_records",
    "criterion_evaluations",
    "acceptances",
    "goal_resolutions",
    "validity_witnesses",
    "observations",
    "justification_sets",
    "support_members",
    "validity_epochs",
    "validity_dirty",
    "operation_identities",
    "operation_bindings",
    "plan_read_sets",
    "plan_commit_receipts",
)

__all__ = ("DDL", "TABLES")
