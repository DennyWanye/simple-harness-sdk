-- 1.1 creation discriminator is independent from a possibly missing assurance binding.
CREATE TABLE assurance_creation_contracts (
 mission_id TEXT PRIMARY KEY NOT NULL REFERENCES missions(mission_id),
 lane TEXT NOT NULL CHECK(lane IN ('LEGACY','COMPLETION_V1','ASSURANCE_1_1')),
 origin TEXT NOT NULL CHECK(origin IN ('FACTORY','MIGRATION_CLASSIFICATION','EXPLICIT_SUCCESSOR')),
 source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
) STRICT;
-- ASSURANCE-EXEC-1.1. Additive side-bindings ONLY; not a full SDK schema.
-- Use the real Store migration runner and the actual next unused descriptor.
-- Keep all previous migration bytes/checksums. No direct production execution.
-- Parent keys must be checked against local dirty source before registration.
CREATE TABLE assurance_mission_bindings (
 mission_id TEXT PRIMARY KEY NOT NULL REFERENCES missions(mission_id),
 contract_version TEXT NOT NULL CHECK(contract_version='assurance-exec-v1.1'),
 policy_hash TEXT NOT NULL CHECK(length(policy_hash)=64 AND policy_hash NOT GLOB '*[^0-9a-f]*'),
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
 activation_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
) STRICT;

-- One logical round -> one package; invocations are bounded children, not new rounds.
CREATE TABLE assurance_review_bindings (
 review_key TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 package_id TEXT NOT NULL UNIQUE REFERENCES review_packages(package_id) DEFERRABLE INITIALLY DEFERRED,
 owner_task_id TEXT NOT NULL REFERENCES tasks(task_id),
 request_command_id TEXT NOT NULL,
 round_no INTEGER NOT NULL CHECK(round_no>=1),
 requirements_revision INTEGER NOT NULL CHECK(requirements_revision>=0),
 requirements_hash TEXT NOT NULL CHECK(length(requirements_hash)=64 AND requirements_hash NOT GLOB '*[^0-9a-f]*'),
 subject_hash TEXT NOT NULL CHECK(length(subject_hash)=64 AND subject_hash NOT GLOB '*[^0-9a-f]*'),
 input_manifest_hash TEXT NOT NULL CHECK(length(input_manifest_hash)=64 AND input_manifest_hash NOT GLOB '*[^0-9a-f]*'),
 binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64 AND binding_hash NOT GLOB '*[^0-9a-f]*'),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 source_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(mission_id,request_command_id,round_no),
 UNIQUE(mission_id,review_key),
 FOREIGN KEY(mission_id,requirements_revision) REFERENCES requirements_revisions(mission_id,revision)
) STRICT;
CREATE INDEX assurance_review_subject_idx ON assurance_review_bindings(mission_id,subject_hash);

-- A semantic check projection over an actual execution receipt, not another execution ledger.
CREATE TABLE assurance_check_bindings (
 check_binding_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 execution_ref_hash TEXT NOT NULL CHECK(length(execution_ref_hash)=64 AND execution_ref_hash NOT GLOB '*[^0-9a-f]*'),
 check_spec_hash TEXT NOT NULL CHECK(length(check_spec_hash)=64 AND check_spec_hash NOT GLOB '*[^0-9a-f]*'),
 subject_hash TEXT NOT NULL CHECK(length(subject_hash)=64 AND subject_hash NOT GLOB '*[^0-9a-f]*'),
 assertion_key TEXT NOT NULL CHECK(length(assertion_key)>0),
 binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64 AND binding_hash NOT GLOB '*[^0-9a-f]*'),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 import_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(mission_id,execution_ref_hash,check_spec_hash,subject_hash,assertion_key),
 UNIQUE(mission_id,check_binding_id)
) STRICT;
CREATE INDEX assurance_check_subject_idx ON assurance_check_bindings(mission_id,subject_hash,check_spec_hash);

CREATE TABLE assurance_review_record_bindings (
 record_id TEXT PRIMARY KEY NOT NULL REFERENCES review_records(record_id) DEFERRABLE INITIALLY DEFERRED,
 mission_id TEXT NOT NULL,
 review_key TEXT NOT NULL,
 source_turn_ref_hash TEXT NOT NULL CHECK(length(source_turn_ref_hash)=64 AND source_turn_ref_hash NOT GLOB '*[^0-9a-f]*'),
 raw_output_hash TEXT NOT NULL CHECK(length(raw_output_hash)=64 AND raw_output_hash NOT GLOB '*[^0-9a-f]*'),
 evidence_manifest_hash TEXT NOT NULL CHECK(length(evidence_manifest_hash)=64 AND evidence_manifest_hash NOT GLOB '*[^0-9a-f]*'),
 binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64 AND binding_hash NOT GLOB '*[^0-9a-f]*'),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 import_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(mission_id,review_key,source_turn_ref_hash),
 FOREIGN KEY(mission_id,review_key) REFERENCES assurance_review_bindings(mission_id,review_key)
) STRICT;

-- Immutable use-specific proof. This is not the current truth table.
CREATE TABLE assurance_use_certificates (
 certificate_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 consumer_kind TEXT NOT NULL,
 consumer_id TEXT NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN ('PLAN','START','MAINTAIN','ACCEPT','CONTEXT','DISCLOSE','RECOVERY')),
 scope_id TEXT NOT NULL,
 read_set_hash TEXT NOT NULL CHECK(length(read_set_hash)=64 AND read_set_hash NOT GLOB '*[^0-9a-f]*'),
 certificate_hash TEXT NOT NULL CHECK(length(certificate_hash)=64 AND certificate_hash NOT GLOB '*[^0-9a-f]*'),
 certificate_json TEXT NOT NULL CHECK(json_valid(certificate_json)),
 issued_at_ms INTEGER NOT NULL CHECK(issued_at_ms>=0),
 not_after_ms INTEGER,
 CHECK(not_after_ms IS NULL OR not_after_ms>=0),
 CHECK(json_extract(certificate_json,'$.decision') IS NOT 'USABLE' OR not_after_ms IS NULL OR not_after_ms>issued_at_ms),
 UNIQUE(mission_id,certificate_id)
) STRICT;
CREATE INDEX assurance_certificate_consumer_idx
 ON assurance_use_certificates(mission_id,consumer_kind,consumer_id,purpose,issued_at_ms);

-- Rebuildable reverse index from the exact certificate JSON, not independent evidence.
CREATE TABLE assurance_dependency_index (
 mission_id TEXT NOT NULL,
 certificate_id TEXT NOT NULL,
 dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('OBJECT','QUERY_SET','ACCESS','POLICY')),
 dependency_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
 PRIMARY KEY(certificate_id,dependency_kind,dependency_key),
 FOREIGN KEY(mission_id,certificate_id) REFERENCES assurance_use_certificates(mission_id,certificate_id)
) STRICT;
CREATE INDEX assurance_dependency_reverse_idx
 ON assurance_dependency_index(mission_id,dependency_kind,dependency_key);

-- Original events are the durable work source. No second outbox or Agent scheduler.
CREATE TABLE assurance_event_cursors (
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 consumer TEXT NOT NULL CHECK(consumer IN ('REVIEW','VALIDITY','CLOSEOUT','NOTIFY')),
 last_event_seq INTEGER NOT NULL CHECK(last_event_seq>=0),
 row_version INTEGER NOT NULL CHECK(row_version>=1),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0),
 PRIMARY KEY(mission_id,consumer)
) STRICT;

-- Derived closeout workflow; the original missions row remains final-status authority.
CREATE TABLE assurance_closeouts (
 mission_id TEXT PRIMARY KEY NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 resolution_id TEXT NOT NULL REFERENCES goal_resolutions(resolution_id),
 state TEXT NOT NULL CHECK(state IN ('NOT_READY','DRAINING','BLOCKED_UNKNOWN','READY','FINALIZED')),
 row_version INTEGER NOT NULL CHECK(row_version>=1),
 check_body_hash TEXT NOT NULL CHECK(length(check_body_hash)=64 AND check_body_hash NOT GLOB '*[^0-9a-f]*'),
 check_body_json TEXT NOT NULL CHECK(json_valid(check_body_json)),
 last_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
) STRICT;

CREATE TRIGGER assurance_review_mission_guard BEFORE INSERT ON assurance_review_bindings
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM review_packages p WHERE p.package_id=NEW.package_id AND p.mission_id=NEW.mission_id)
  THEN RAISE(ABORT,'review package mission mismatch') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM tasks t WHERE t.task_id=NEW.owner_task_id AND t.mission_id=NEW.mission_id)
  THEN RAISE(ABORT,'review owner mission mismatch') END;
END;
CREATE TRIGGER assurance_record_mission_guard BEFORE INSERT ON assurance_review_record_bindings
BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM review_records r JOIN assurance_review_bindings b ON b.package_id=r.package_id
  WHERE r.record_id=NEW.record_id AND r.mission_id=NEW.mission_id AND b.review_key=NEW.review_key AND b.mission_id=NEW.mission_id)
  THEN RAISE(ABORT,'review record binding mismatch') END;
END;
CREATE TRIGGER assurance_closeout_mission_guard BEFORE INSERT ON assurance_closeouts
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM goal_resolutions r WHERE r.resolution_id=NEW.resolution_id AND r.mission_id=NEW.mission_id)
  THEN RAISE(ABORT,'closeout resolution mission mismatch') END;
END;
CREATE TRIGGER assurance_closeout_update_guard BEFORE UPDATE ON assurance_closeouts
BEGIN
 SELECT CASE WHEN NEW.mission_id<>OLD.mission_id OR NEW.row_version<>OLD.row_version+1 OR OLD.state='FINALIZED'
  OR (NEW.state='FINALIZED' AND OLD.state<>'READY')
  THEN RAISE(ABORT,'invalid closeout update') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM goal_resolutions r WHERE r.resolution_id=NEW.resolution_id AND r.mission_id=NEW.mission_id)
  THEN RAISE(ABORT,'closeout resolution mission mismatch') END;
END;
CREATE TRIGGER assurance_cursor_update_guard BEFORE UPDATE ON assurance_event_cursors
BEGIN
 SELECT CASE WHEN NEW.mission_id<>OLD.mission_id OR NEW.consumer<>OLD.consumer
  OR NEW.last_event_seq<OLD.last_event_seq OR NEW.row_version<>OLD.row_version+1
  THEN RAISE(ABORT,'invalid cursor update') END;
END;

CREATE TRIGGER assurance_mission_bindings_no_update BEFORE UPDATE ON assurance_mission_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_mission_bindings_no_delete BEFORE DELETE ON assurance_mission_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_review_bindings_no_update BEFORE UPDATE ON assurance_review_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_review_bindings_no_delete BEFORE DELETE ON assurance_review_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_check_bindings_no_update BEFORE UPDATE ON assurance_check_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_check_bindings_no_delete BEFORE DELETE ON assurance_check_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_review_record_bindings_no_update BEFORE UPDATE ON assurance_review_record_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_review_record_bindings_no_delete BEFORE DELETE ON assurance_review_record_bindings
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_use_certificates_no_update BEFORE UPDATE ON assurance_use_certificates
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

CREATE TRIGGER assurance_use_certificates_no_delete BEFORE DELETE ON assurance_use_certificates
BEGIN
 SELECT RAISE(ABORT,'immutable assurance binding');
END;

-- Durable CAS references. Not a copy of blobs, tasks, or invocation state.
CREATE TABLE assurance_blob_pins (
 pin_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 review_key TEXT NOT NULL,
 blob_hash TEXT NOT NULL CHECK(length(blob_hash)=64 AND blob_hash NOT GLOB '*[^0-9a-f]*'),
 object_ref_json TEXT NOT NULL CHECK(json_valid(object_ref_json)),
 state TEXT NOT NULL CHECK(state IN ('PREPARING','BOUND','RELEASED')),
 row_version INTEGER NOT NULL CHECK(row_version>=1),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 released_at_ms INTEGER,
 source_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 last_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(mission_id,review_key,blob_hash),
 CHECK((state='RELEASED' AND released_at_ms IS NOT NULL AND released_at_ms>=created_at_ms)
   OR (state<>'RELEASED' AND released_at_ms IS NULL))
) STRICT;
CREATE INDEX assurance_blob_live_idx ON assurance_blob_pins(blob_hash,state);
CREATE TRIGGER assurance_blob_pin_transition BEFORE UPDATE ON assurance_blob_pins
BEGIN
 SELECT CASE WHEN NEW.pin_id<>OLD.pin_id OR NEW.mission_id<>OLD.mission_id
  OR NEW.review_key<>OLD.review_key OR NEW.blob_hash<>OLD.blob_hash
  OR NEW.object_ref_json<>OLD.object_ref_json OR NEW.source_receipt_id<>OLD.source_receipt_id
  OR NEW.created_at_ms<>OLD.created_at_ms OR NEW.row_version<>OLD.row_version+1
  OR NOT ((OLD.state='PREPARING' AND NEW.state IN ('BOUND','RELEASED'))
       OR (OLD.state='BOUND' AND NEW.state='RELEASED'))
  THEN RAISE(ABORT,'invalid assurance blob pin transition') END;
END;
CREATE TRIGGER assurance_blob_pin_no_delete BEFORE DELETE ON assurance_blob_pins
BEGIN SELECT RAISE(ABORT,'retain pin release history'); END;

-- Approved mapping from existing Requirements evidence policy; not another Requirements.
CREATE TABLE assurance_criterion_policies (
 policy_id TEXT PRIMARY KEY NOT NULL,
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 requirements_revision INTEGER NOT NULL,
 requirements_hash TEXT NOT NULL CHECK(length(requirements_hash)=64),
 scope_hash TEXT NOT NULL CHECK(length(scope_hash)=64),
 policy_hash TEXT NOT NULL CHECK(length(policy_hash)=64),
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
 approval_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(mission_id,requirements_revision,scope_hash),
 FOREIGN KEY(mission_id,requirements_revision) REFERENCES requirements_revisions(mission_id,revision)
) STRICT;
CREATE TABLE assurance_review_invocations (
 review_key TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal IN (1,2)),
 mission_id TEXT NOT NULL,
 dispatch_intent_id TEXT NOT NULL UNIQUE REFERENCES dispatch_intents(intent_id) DEFERRABLE INITIALLY DEFERRED,
 invocation_hash TEXT NOT NULL CHECK(length(invocation_hash)=64),
 invocation_json TEXT NOT NULL CHECK(json_valid(invocation_json)),
 reservation_event_id TEXT NOT NULL REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED,
 source_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED,
 PRIMARY KEY(review_key,ordinal),
 FOREIGN KEY(mission_id,review_key) REFERENCES assurance_review_bindings(mission_id,review_key)
) STRICT;
CREATE TRIGGER assurance_invocation_mission BEFORE INSERT ON assurance_review_invocations
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM dispatch_intents WHERE intent_id=NEW.dispatch_intent_id AND mission_id=NEW.mission_id)
  THEN RAISE(ABORT,'invocation dispatch mission mismatch') END;
 SELECT CASE WHEN NEW.ordinal=2 AND NOT EXISTS(SELECT 1 FROM assurance_review_invocations WHERE review_key=NEW.review_key AND ordinal=1)
  THEN RAISE(ABORT,'format repair lacks initial invocation') END;
END;
CREATE TABLE assurance_disclosure_batches (
 review_key TEXT NOT NULL,
 batch_no INTEGER NOT NULL CHECK(batch_no>=0),
 mission_id TEXT NOT NULL,
 previous_hash TEXT,
 batch_hash TEXT NOT NULL CHECK(length(batch_hash)=64),
 batch_json TEXT NOT NULL CHECK(json_valid(batch_json)),
 disclosure_event_id TEXT NOT NULL REFERENCES events(event_id),
 PRIMARY KEY(review_key,batch_no),
 UNIQUE(review_key,batch_no,batch_hash),
 FOREIGN KEY(mission_id,review_key) REFERENCES assurance_review_bindings(mission_id,review_key),
 CHECK((batch_no=0 AND previous_hash IS NULL) OR (batch_no>0 AND length(previous_hash)=64))
) STRICT;
CREATE TRIGGER assurance_disclosure_chain BEFORE INSERT ON assurance_disclosure_batches
BEGIN
 SELECT CASE WHEN NEW.batch_no>0 AND NOT EXISTS(
  SELECT 1 FROM assurance_disclosure_batches WHERE review_key=NEW.review_key
   AND batch_no=NEW.batch_no-1 AND batch_hash=NEW.previous_hash)
  THEN RAISE(ABORT,'disclosure chain mismatch') END;
END;
-- Event inbox scheduling metadata. Original dispatch_intents still owns execution.
CREATE TABLE assurance_pending_work (
 mission_id TEXT NOT NULL REFERENCES assurance_mission_bindings(mission_id),
 consumer TEXT NOT NULL CHECK(consumer IN ('REVIEW','VALIDITY','CLOSEOUT','NOTIFY')),
 work_key TEXT NOT NULL,
 trigger_event_id TEXT NOT NULL REFERENCES events(event_id),
 target_epoch INTEGER NOT NULL CHECK(target_epoch>=0),
 target_fingerprint TEXT NOT NULL CHECK(length(target_fingerprint)=64),
 state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','WAITING','DONE','REJECTED')),
 row_version INTEGER NOT NULL CHECK(row_version>=1),
 tries INTEGER NOT NULL CHECK(tries>=0),
 rechecks INTEGER NOT NULL DEFAULT 0 CHECK(rechecks>=0),
 recheck_started_at_ms INTEGER CHECK(recheck_started_at_ms>=0),
 not_before_ms INTEGER NOT NULL CHECK(not_before_ms>=0),
 wait_reason TEXT,
 owner TEXT,
 lease_until_ms INTEGER,
 PRIMARY KEY(mission_id,consumer,work_key),
 CHECK((state='RUNNING' AND owner IS NOT NULL AND lease_until_ms IS NOT NULL)
  OR (state<>'RUNNING' AND owner IS NULL AND lease_until_ms IS NULL))
) STRICT;
CREATE INDEX assurance_due_work ON assurance_pending_work(state,not_before_ms);
CREATE TRIGGER assurance_pending_insert BEFORE INSERT ON assurance_pending_work
BEGIN SELECT CASE WHEN NEW.state<>'PENDING' OR NEW.row_version<>1 OR NEW.tries<>0
 THEN RAISE(ABORT,'invalid pending initial state') END; END;
CREATE TRIGGER assurance_pending_update BEFORE UPDATE ON assurance_pending_work
BEGIN
 SELECT CASE WHEN NEW.mission_id<>OLD.mission_id OR NEW.consumer<>OLD.consumer OR NEW.work_key<>OLD.work_key
  OR NEW.row_version<>OLD.row_version+1 OR NEW.target_epoch<OLD.target_epoch OR NEW.tries<OLD.tries
  OR NEW.rechecks<OLD.rechecks
  OR (OLD.recheck_started_at_ms IS NOT NULL AND NEW.recheck_started_at_ms IS NOT OLD.recheck_started_at_ms)
  OR NOT (
    (OLD.state='PENDING' AND NEW.state IN ('PENDING','RUNNING','REJECTED')) OR
    (OLD.state='RUNNING' AND NEW.state IN ('DONE','WAITING','REJECTED','PENDING')) OR
    (OLD.state='WAITING' AND NEW.state IN ('WAITING','PENDING','RUNNING','REJECTED')) OR
    (OLD.state IN ('DONE','REJECTED') AND NEW.state='PENDING' AND NEW.target_epoch>OLD.target_epoch)
  ) THEN RAISE(ABORT,'invalid work transition') END;
END;

CREATE TRIGGER assurance_pending_no_delete BEFORE DELETE ON assurance_pending_work
BEGIN SELECT RAISE(ABORT,'retain work budget and target history'); END;
CREATE TRIGGER assurance_cursor_no_replace BEFORE INSERT ON assurance_event_cursors
WHEN EXISTS(SELECT 1 FROM assurance_event_cursors
 WHERE mission_id=NEW.mission_id AND consumer=NEW.consumer)
BEGIN SELECT RAISE(ABORT,'duplicate cursor; use CAS update'); END;
-- Global imported access/policy barrier + persistent clock rollback detector, not a permission store.
CREATE TABLE assurance_environment_state (
 singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton=1),
 epoch INTEGER NOT NULL CHECK(epoch>=0),
 clock_generation INTEGER NOT NULL CHECK(clock_generation>=0),
 wall_high_ms INTEGER NOT NULL CHECK(wall_high_ms>=0),
 clock_state TEXT NOT NULL CHECK(clock_state IN ('STABLE','ROLLBACK')),
 row_version INTEGER NOT NULL CHECK(row_version>=1),
 change_receipt_id TEXT NOT NULL REFERENCES commit_receipts(commit_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TRIGGER assurance_environment_update BEFORE UPDATE ON assurance_environment_state
BEGIN SELECT CASE WHEN NEW.singleton<>OLD.singleton OR NEW.epoch<OLD.epoch
 OR NEW.clock_generation<OLD.clock_generation OR NEW.wall_high_ms<OLD.wall_high_ms
 OR NEW.row_version<>OLD.row_version+1
 THEN RAISE(ABORT,'environment counter regression') END; END;
-- F11: state-machine constraints are enforced for INSERT and DELETE too.
CREATE TRIGGER assurance_closeout_initial BEFORE INSERT ON assurance_closeouts
BEGIN SELECT CASE WHEN NEW.state<>'NOT_READY' OR NEW.row_version<>1
 THEN RAISE(ABORT,'closeout must begin NOT_READY v1') END; END;
CREATE TRIGGER assurance_closeout_no_delete BEFORE DELETE ON assurance_closeouts
BEGIN SELECT RAISE(ABORT,'retain closeout history'); END;
CREATE TRIGGER assurance_blob_pin_initial BEFORE INSERT ON assurance_blob_pins
BEGIN SELECT CASE WHEN NEW.state<>'PREPARING' OR NEW.row_version<>1
 THEN RAISE(ABORT,'pin must begin PREPARING v1') END; END;
CREATE TRIGGER assurance_blob_pin_bound_review BEFORE UPDATE ON assurance_blob_pins
WHEN NEW.state='BOUND'
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM assurance_review_bindings
  WHERE review_key=NEW.review_key AND mission_id=NEW.mission_id)
 THEN RAISE(ABORT,'BOUND pin lacks same-mission review') END;
END;
CREATE TRIGGER assurance_activation_lane BEFORE INSERT ON assurance_mission_bindings
BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM assurance_creation_contracts
 WHERE mission_id=NEW.mission_id AND lane='ASSURANCE_1_1')
 THEN RAISE(ABORT,'assurance lane not established') END; END;

CREATE TRIGGER assurance_creation_contracts_no_update BEFORE UPDATE ON assurance_creation_contracts
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_creation_contracts_no_delete BEFORE DELETE ON assurance_creation_contracts
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_criterion_policies_no_update BEFORE UPDATE ON assurance_criterion_policies
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_criterion_policies_no_delete BEFORE DELETE ON assurance_criterion_policies
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_review_invocations_no_update BEFORE UPDATE ON assurance_review_invocations
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_review_invocations_no_delete BEFORE DELETE ON assurance_review_invocations
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_disclosure_batches_no_update BEFORE UPDATE ON assurance_disclosure_batches
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_disclosure_batches_no_delete BEFORE DELETE ON assurance_disclosure_batches
BEGIN SELECT RAISE(ABORT,'immutable assurance binding'); END;

CREATE TRIGGER assurance_closeouts_no_replace BEFORE INSERT ON assurance_closeouts
WHEN EXISTS(SELECT 1 FROM assurance_closeouts WHERE mission_id=NEW.mission_id)
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_blob_pins_no_replace BEFORE INSERT ON assurance_blob_pins
WHEN EXISTS(SELECT 1 FROM assurance_blob_pins WHERE pin_id=NEW.pin_id OR (mission_id=NEW.mission_id AND review_key=NEW.review_key AND blob_hash=NEW.blob_hash))
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_mission_bindings_no_replace BEFORE INSERT ON assurance_mission_bindings
WHEN EXISTS(SELECT 1 FROM assurance_mission_bindings WHERE mission_id=NEW.mission_id)
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_review_bindings_no_replace BEFORE INSERT ON assurance_review_bindings
WHEN EXISTS(SELECT 1 FROM assurance_review_bindings WHERE review_key=NEW.review_key OR package_id=NEW.package_id OR (mission_id=NEW.mission_id AND request_command_id=NEW.request_command_id AND round_no=NEW.round_no))
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_review_invocations_no_replace BEFORE INSERT ON assurance_review_invocations
WHEN EXISTS(SELECT 1 FROM assurance_review_invocations WHERE (review_key=NEW.review_key AND ordinal=NEW.ordinal) OR dispatch_intent_id=NEW.dispatch_intent_id)
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_disclosure_batches_no_replace BEFORE INSERT ON assurance_disclosure_batches
WHEN EXISTS(SELECT 1 FROM assurance_disclosure_batches WHERE review_key=NEW.review_key AND batch_no=NEW.batch_no)
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_creation_contracts_no_replace BEFORE INSERT ON assurance_creation_contracts
WHEN EXISTS(SELECT 1 FROM assurance_creation_contracts WHERE mission_id=NEW.mission_id)
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_criterion_policies_no_replace BEFORE INSERT ON assurance_criterion_policies
WHEN EXISTS(SELECT 1 FROM assurance_criterion_policies WHERE policy_id=NEW.policy_id OR (mission_id=NEW.mission_id AND requirements_revision=NEW.requirements_revision AND scope_hash=NEW.scope_hash))
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_check_bindings_no_replace BEFORE INSERT ON assurance_check_bindings
WHEN EXISTS(SELECT 1 FROM assurance_check_bindings WHERE check_binding_id=NEW.check_binding_id OR (mission_id=NEW.mission_id AND execution_ref_hash=NEW.execution_ref_hash AND check_spec_hash=NEW.check_spec_hash AND subject_hash=NEW.subject_hash AND assertion_key=NEW.assertion_key))
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_review_record_bindings_no_replace BEFORE INSERT ON assurance_review_record_bindings
WHEN EXISTS(SELECT 1 FROM assurance_review_record_bindings WHERE record_id=NEW.record_id OR (mission_id=NEW.mission_id AND review_key=NEW.review_key AND source_turn_ref_hash=NEW.source_turn_ref_hash))
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_use_certificates_no_replace BEFORE INSERT ON assurance_use_certificates
WHEN EXISTS(SELECT 1 FROM assurance_use_certificates WHERE certificate_id=NEW.certificate_id)
BEGIN SELECT RAISE(ABORT,'duplicate identity; use original receipt'); END;

CREATE TRIGGER assurance_environment_no_delete BEFORE DELETE ON assurance_environment_state
BEGIN SELECT RAISE(ABORT,'environment high water cannot be deleted'); END;
CREATE TRIGGER assurance_environment_no_replace BEFORE INSERT ON assurance_environment_state
WHEN EXISTS(SELECT 1 FROM assurance_environment_state WHERE singleton=NEW.singleton)
BEGIN SELECT RAISE(ABORT,'environment cannot be replaced'); END;

-- R02: conflicting equal targets must abort, including direct SQL callers.
CREATE TRIGGER assurance_manifest_relocation BEFORE INSERT ON input_manifests
WHEN EXISTS(SELECT 1 FROM input_manifests m WHERE m.manifest_hash=NEW.manifest_hash
 AND m.origin_mission_id<>NEW.origin_mission_id AND EXISTS(
 SELECT 1 FROM assurance_mission_bindings b
 WHERE b.mission_id IN (m.origin_mission_id,NEW.origin_mission_id)))
BEGIN SELECT RAISE(ABORT,'ASSURANCE_SOURCE_RELOCATION_FORBIDDEN'); END;

CREATE TRIGGER assurance_aggregate_no_replace BEFORE INSERT ON validity_epochs
WHEN NEW.scope_id='assurance:mission' AND EXISTS(SELECT 1 FROM validity_epochs
 WHERE mission_id=NEW.mission_id AND scope_id=NEW.scope_id)
BEGIN SELECT RAISE(ABORT,'retain assurance aggregate epoch'); END;
CREATE TRIGGER assurance_aggregate_no_delete BEFORE DELETE ON validity_epochs
WHEN OLD.scope_id='assurance:mission'
BEGIN SELECT RAISE(ABORT,'retain assurance aggregate epoch'); END;
CREATE TRIGGER assurance_aggregate_monotone BEFORE UPDATE ON validity_epochs
WHEN OLD.scope_id='assurance:mission' OR NEW.scope_id='assurance:mission'
BEGIN SELECT CASE WHEN OLD.mission_id<>NEW.mission_id OR OLD.scope_id<>NEW.scope_id
 OR NEW.epoch<>OLD.epoch+1 THEN RAISE(ABORT,'invalid assurance aggregate epoch') END; END;

-- BEFORE INSERT still runs for REPLACE with recursive_triggers=OFF. Preserve
-- the old result's Mission when a globally keyed verification is relocated.
CREATE TRIGGER assurance_verification_relocation BEFORE INSERT ON verifications
WHEN EXISTS(SELECT 1 FROM verifications v JOIN results r ON r.result_id=v.result_id
 JOIN results n ON n.result_id=NEW.result_id
 WHERE v.verification_id=NEW.verification_id AND r.mission_id<>n.mission_id
 AND EXISTS(SELECT 1 FROM assurance_mission_bindings b
 WHERE b.mission_id IN (r.mission_id,n.mission_id)))
BEGIN SELECT RAISE(ABORT,'ASSURANCE_SOURCE_RELOCATION_FORBIDDEN'); END;

CREATE TRIGGER assurance_pending_target_conflict BEFORE UPDATE ON assurance_pending_work
WHEN NEW.target_epoch=OLD.target_epoch AND NEW.target_fingerprint<>OLD.target_fingerprint
BEGIN SELECT RAISE(ABORT,'WORK_TARGET_CONFLICT'); END;

-- This implementation merges with CAS UPDATE, never UPSERT/REPLACE. Prevent
-- REPLACE from silently deleting a claim or resetting tries/row_version.
CREATE TRIGGER assurance_pending_no_replace BEFORE INSERT ON assurance_pending_work
WHEN EXISTS(SELECT 1 FROM assurance_pending_work WHERE mission_id=NEW.mission_id
 AND consumer=NEW.consumer AND work_key=NEW.work_key)
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM assurance_pending_work
  WHERE mission_id=NEW.mission_id AND consumer=NEW.consumer AND work_key=NEW.work_key
   AND target_epoch=NEW.target_epoch AND target_fingerprint<>NEW.target_fingerprint)
  THEN RAISE(ABORT,'WORK_TARGET_CONFLICT') END;
 SELECT RAISE(ABORT,'duplicate work identity; use CAS update');
END;
