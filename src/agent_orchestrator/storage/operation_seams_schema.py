# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Operation payload/intent and repair continuation additions, original Store only."""

DDL = r"""
-- V14-OP-SEAMS-1.0. Additive, register at the actual next unused migration.
-- Owned by existing Store.transaction; do not executescript into a production DB.
-- Parent tables/keys must match the real candidate; this is not a parent fixture.
CREATE TABLE operation_payload_objects (
 object_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 object_kind TEXT NOT NULL CHECK(object_kind IN ('PARAMETERS','EFFECT_CONTRACT','ACTION_PROPOSAL')),
 object_revision INTEGER NOT NULL CHECK(object_revision = 1),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 byte_length INTEGER NOT NULL CHECK(byte_length BETWEEN 1 AND 262144),
 media_type TEXT NOT NULL,
 storage_uri TEXT NOT NULL,
 source_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id)
   DEFERRABLE INITIALLY DEFERRED,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(mission_id,object_id,content_hash)
) STRICT;

CREATE TABLE operation_intent_bindings (
 intent_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 tenant_id TEXT NOT NULL,
 principal_id TEXT NOT NULL,
 scope_id TEXT NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('USER_COMMAND','AUTHORIZED_SLOT')),
 origin_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id)
   DEFERRABLE INITIALLY DEFERRED,
 slot_key TEXT NOT NULL CHECK(length(slot_key)>0),
 submission_receipt_id TEXT NOT NULL UNIQUE REFERENCES commit_receipts(commit_id)
   DEFERRABLE INITIALLY DEFERRED,
 submission_hash TEXT NOT NULL CHECK(length(submission_hash)=64),
 supersedes_intent_id TEXT REFERENCES operation_intent_bindings(intent_id),
 producer_task_id TEXT NOT NULL REFERENCES tasks(task_id),
 producer_occurrence_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,
 source_result_id TEXT NOT NULL REFERENCES results(result_id),
 source_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 candidate_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 candidate_file_hash TEXT NOT NULL CHECK(length(candidate_file_hash)=64),
 task_contract_revision INTEGER NOT NULL CHECK(task_contract_revision>=0),
 task_contract_hash TEXT NOT NULL CHECK(length(task_contract_hash)=64),
 requirements_revision INTEGER NOT NULL CHECK(requirements_revision>=0),
 requirements_hash TEXT NOT NULL CHECK(length(requirements_hash)=64),
 plan_revision INTEGER NOT NULL CHECK(plan_revision>=0),
 plan_snapshot_hash TEXT NOT NULL CHECK(length(plan_snapshot_hash)=64),
 parameters_object_id TEXT NOT NULL,
 parameters_content_hash TEXT NOT NULL CHECK(length(parameters_content_hash)=64),
 params_hash TEXT NOT NULL CHECK(length(params_hash)=64),
 effect_object_id TEXT NOT NULL,
 effect_content_hash TEXT NOT NULL CHECK(length(effect_content_hash)=64),
 proposal_object_id TEXT NOT NULL,
 proposal_content_hash TEXT NOT NULL CHECK(length(proposal_content_hash)=64),
 request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 review_package_id TEXT NOT NULL UNIQUE REFERENCES review_packages(package_id),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(mission_id,origin_receipt_id,slot_key),
 CHECK(supersedes_intent_id IS NULL OR supersedes_intent_id<>intent_id),
 FOREIGN KEY(mission_id,parameters_object_id,parameters_content_hash)
   REFERENCES operation_payload_objects(mission_id,object_id,content_hash),
 FOREIGN KEY(mission_id,effect_object_id,effect_content_hash)
   REFERENCES operation_payload_objects(mission_id,object_id,content_hash),
 FOREIGN KEY(mission_id,proposal_object_id,proposal_content_hash)
   REFERENCES operation_payload_objects(mission_id,object_id,content_hash),
 FOREIGN KEY(mission_id,requirements_revision)
   REFERENCES requirements_revisions(mission_id,revision),
 FOREIGN KEY(mission_id,plan_revision) REFERENCES plan_revisions(mission_id,revision)
) STRICT;
CREATE INDEX operation_intent_mission_idx ON operation_intent_bindings(mission_id,intent_id);

CREATE TABLE planning_repair_continuations (
 continuation_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 decision_id TEXT NOT NULL UNIQUE REFERENCES planning_decisions(decision_id),
 request_id TEXT NOT NULL REFERENCES planning_requests(request_id),
 command_id TEXT NOT NULL UNIQUE,
 raw_artifact_ref_json TEXT NOT NULL CHECK(json_valid(raw_artifact_ref_json)),
 raw_hash TEXT NOT NULL CHECK(length(raw_hash)=64),
 decision_hash TEXT NOT NULL CHECK(length(decision_hash)=64),
 codec_version TEXT NOT NULL,
 protocol_version TEXT NOT NULL CHECK(protocol_version='planning-decision-v1'),
 package_hash TEXT NOT NULL CHECK(length(package_hash)=64),
 prompt_hash TEXT NOT NULL CHECK(length(prompt_hash)=64),
 base_plan_revision INTEGER NOT NULL CHECK(base_plan_revision>=0),
 requirements_revision INTEGER NOT NULL CHECK(requirements_revision>=0),
 authority_binding_json TEXT NOT NULL CHECK(json_valid(authority_binding_json)),
 targets_json TEXT NOT NULL CHECK(json_valid(targets_json) AND json_type(targets_json)='array'),
 last_preview_hash TEXT NOT NULL CHECK(length(last_preview_hash)=64),
 state TEXT NOT NULL CHECK(state IN
   ('WAITING','READY','PAUSED','MANUAL_REQUIRED','STALE_FENCED','APPLIED','CLOSED')),
 row_version INTEGER NOT NULL CHECK(row_version>=1),
 policy_version TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 deadline_ms INTEGER NOT NULL CHECK(deadline_ms>=created_at_ms),
 resume_limit INTEGER NOT NULL CHECK(resume_limit>=1),
 resume_count INTEGER NOT NULL CHECK(resume_count>=0 AND resume_count<=resume_limit),
 next_check_at_ms INTEGER NOT NULL CHECK(next_check_at_ms>=0),
 owner_id TEXT,
 lease_until_ms INTEGER,
 last_reason_code TEXT,
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((owner_id IS NULL AND lease_until_ms IS NULL) OR
       (owner_id IS NOT NULL AND lease_until_ms IS NOT NULL AND lease_until_ms>=0))
) STRICT;
CREATE INDEX planning_repair_due_idx ON planning_repair_continuations(state,next_check_at_ms);
CREATE INDEX planning_repair_mission_idx ON planning_repair_continuations(mission_id,state);

"""
