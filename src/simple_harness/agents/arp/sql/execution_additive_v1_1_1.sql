-- ARP-EXEC-1.1.1. Fresh additive descriptor only, not an upgrade over applied 1.0 bytes.
-- Every connection: foreign_keys=ON; recursive_triggers=ON, read back before txn.
-- ARP-EXEC-1.0: additive execution.db side bindings, not orchestrator.db.
-- Register through ORIGINAL execution UOW migration runner, next free descriptor.
-- Requires the actual base_agent_bindings_v1 / base_agent_turns_v1 keys.
-- No COMMIT/BEGIN/executescript: caller owns one migration transaction.
CREATE TABLE arp_profiles (
 profile_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 body_hash TEXT NOT NULL CHECK(length(body_hash)=64), body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 activation_receipt_json TEXT NOT NULL CHECK(json_valid(activation_receipt_json)),
 PRIMARY KEY(profile_id,revision), UNIQUE(profile_id,revision,body_hash)
) STRICT;
CREATE TABLE arp_agent_protocols (
 agent_id TEXT PRIMARY KEY NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 protocol TEXT NOT NULL CHECK(protocol IN ('LEGACY_AT_MIGRATION','LEGACY_EXPLICIT','ARP_V1','ARP_V1_1','ARP_V1_1_1')),
 creation_receipt_ref_json TEXT NOT NULL CHECK(json_valid(creation_receipt_ref_json)),
 marker_hash TEXT NOT NULL CHECK(length(marker_hash)=64),
 marked_at_ms INTEGER NOT NULL CHECK(marked_at_ms>=0)
) STRICT;
CREATE TABLE arp_agent_sessions (
 session_id TEXT PRIMARY KEY NOT NULL,
 agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id),
 profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL, profile_hash TEXT NOT NULL,
 creation_root_id TEXT NOT NULL, root_incarnation TEXT NOT NULL,
 creation_key TEXT NOT NULL UNIQUE, create_command_hash TEXT NOT NULL CHECK(length(create_command_hash)=64),
 relative_directory TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('CREATING','ACTIVE','DRAINING','PURGING','PURGED','QUARANTINED')),
 generation INTEGER NOT NULL CHECK(generation>0), row_version INTEGER NOT NULL CHECK(row_version>0),
 journal_seq_from INTEGER NOT NULL CHECK(journal_seq_from>0),
 sealed_highwater INTEGER CHECK(sealed_highwater IS NULL OR sealed_highwater>=journal_seq_from-1),
 destroy_command_id TEXT UNIQUE, destroy_command_hash TEXT,
 purge_progress_json TEXT CHECK(purge_progress_json IS NULL OR (json_valid(purge_progress_json) AND json_type(purge_progress_json)='object')),
 purge_progress_hash TEXT CHECK(purge_progress_hash IS NULL OR length(purge_progress_hash)=64),
 delete_proof_ref_json TEXT CHECK(delete_proof_ref_json IS NULL OR json_valid(delete_proof_ref_json)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 FOREIGN KEY(profile_id,profile_revision,profile_hash) REFERENCES arp_profiles(profile_id,revision,body_hash),
 UNIQUE(session_id,agent_id), CHECK((destroy_command_id IS NULL)=(destroy_command_hash IS NULL)),
 CHECK((purge_progress_json IS NULL)=(purge_progress_hash IS NULL)),
 CHECK(state NOT IN ('PURGING','PURGED') OR (sealed_highwater IS NOT NULL AND delete_proof_ref_json IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX arp_one_live_session_per_agent ON arp_agent_sessions(agent_id) WHERE state!='PURGED';
CREATE TABLE arp_context_policy_adoptions (
 session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id),
 adoption_revision INTEGER NOT NULL CHECK(adoption_revision>0),
 policy_ref_json TEXT NOT NULL CHECK(json_valid(policy_ref_json)),
 policy_body_hash TEXT NOT NULL CHECK(length(policy_body_hash)=64),
 command_id TEXT NOT NULL UNIQUE, command_hash TEXT NOT NULL CHECK(length(command_hash)=64),
 authority_receipt_ref_json TEXT NOT NULL CHECK(json_valid(authority_receipt_ref_json)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)),
 adopted_at_ms INTEGER NOT NULL CHECK(adopted_at_ms>=0),
 PRIMARY KEY(session_id,adoption_revision)
) STRICT;
CREATE TABLE arp_context_requests (
 context_id TEXT PRIMARY KEY NOT NULL,
 session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 turn_id TEXT NOT NULL REFERENCES base_agent_turns_v1(turn_id),
 provider_request_ordinal INTEGER NOT NULL CHECK(provider_request_ordinal>=0),
 session_generation INTEGER NOT NULL CHECK(session_generation>0), journal_highwater INTEGER NOT NULL CHECK(journal_highwater>=0),
 manifest_hash TEXT NOT NULL CHECK(length(manifest_hash)=64), manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
 planned_request_hash TEXT NOT NULL CHECK(length(planned_request_hash)=64),
 original_request_key TEXT NOT NULL UNIQUE,
 input_charge INTEGER NOT NULL CHECK(input_charge>=0), input_budget INTEGER NOT NULL CHECK(input_budget>=0),
 output_reserve INTEGER NOT NULL CHECK(output_reserve>0),
 catalog_epoch INTEGER NOT NULL CHECK(catalog_epoch>=0),
 source_read_set_json TEXT NOT NULL CHECK(json_valid(source_read_set_json)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(turn_id,provider_request_ordinal),
 FOREIGN KEY(session_id,agent_id) REFERENCES arp_agent_sessions(session_id,agent_id),
 CHECK(input_charge<=input_budget)
) STRICT;
CREATE TABLE arp_catalog_revisions (
 namespace_id TEXT NOT NULL,
 entry_kind TEXT NOT NULL CHECK(entry_kind IN ('CAPABILITY','PROVIDER','DEPLOYMENT','TOOL','SKILL','SCHEMA','WORKFLOW')),
 entry_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64), body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)),
 bundle_root_ref_json TEXT CHECK(bundle_root_ref_json IS NULL OR json_valid(bundle_root_ref_json)),
 PRIMARY KEY(namespace_id,entry_kind,entry_id,revision), UNIQUE(namespace_id,entry_kind,entry_id,revision,content_hash)
) STRICT;
CREATE TABLE arp_catalog_activation (
 namespace_id TEXT NOT NULL,
 entry_kind TEXT NOT NULL,entry_id TEXT NOT NULL,revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('QUARANTINED','TRIAL','ADMITTED','SUSPENDED','RETIRED')),
 row_version INTEGER NOT NULL CHECK(row_version>0),
 authority_ref_json TEXT NOT NULL CHECK(json_valid(authority_ref_json)),
 evaluation_ref_json TEXT CHECK(evaluation_ref_json IS NULL OR json_valid(evaluation_ref_json)),
 scope_ref_json TEXT NOT NULL CHECK(json_valid(scope_ref_json)),
 PRIMARY KEY(namespace_id,entry_kind,entry_id,revision),
 FOREIGN KEY(namespace_id,entry_kind,entry_id,revision,content_hash) REFERENCES arp_catalog_revisions(namespace_id,entry_kind,entry_id,revision,content_hash)
) STRICT;
CREATE TABLE arp_registry_epoch (
 namespace_id TEXT PRIMARY KEY NOT NULL,
 epoch INTEGER NOT NULL CHECK(epoch>=0), source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json))
) STRICT;
CREATE TABLE arp_capability_bindings (
 binding_id TEXT PRIMARY KEY NOT NULL,
 session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id),
 owner_contract_key TEXT NOT NULL, requirement_key TEXT NOT NULL,
 binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64),body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)),
 UNIQUE(session_id,owner_contract_key,requirement_key,binding_hash)
) STRICT;
CREATE TABLE arp_tool_exposures (
 snapshot_id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id),
 generation INTEGER NOT NULL CHECK(generation>0), catalog_epoch INTEGER NOT NULL CHECK(catalog_epoch>=0),
 snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)),
 UNIQUE(session_id,generation,snapshot_hash)
) STRICT;
CREATE TABLE arp_skill_uses (
 use_id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id),
 turn_id TEXT NOT NULL REFERENCES base_agent_turns_v1(turn_id),
 use_key TEXT NOT NULL, use_hash TEXT NOT NULL CHECK(length(use_hash)=64),body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 original_call_ref_json TEXT CHECK(original_call_ref_json IS NULL OR json_valid(original_call_ref_json)),
 UNIQUE(session_id,use_key)
) STRICT;
CREATE TABLE arp_jobs (
 job_id TEXT PRIMARY KEY NOT NULL,session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id),
 kind TEXT NOT NULL CHECK(kind IN ('INDEX','PURGE','BIND_IMPORT')),
 semantic_key TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
 generation INTEGER NOT NULL CHECK(generation>0),
 state TEXT NOT NULL CHECK(state IN ('PENDING','LEASED','DONE','BLOCKED','CANCELLED')),
 row_version INTEGER NOT NULL CHECK(row_version>0),attempts INTEGER NOT NULL CHECK(attempts>=0),
 next_at_ms INTEGER NOT NULL CHECK(next_at_ms>=0),deadline_ms INTEGER NOT NULL CHECK(deadline_ms>=next_at_ms),
 owner_id TEXT,lease_until_ms INTEGER,
 result_receipt_ref_json TEXT CHECK(result_receipt_ref_json IS NULL OR json_valid(result_receipt_ref_json)),
 UNIQUE(session_id,kind,semantic_key),
 CHECK((state='LEASED')=(owner_id IS NOT NULL AND lease_until_ms IS NOT NULL)),
 CHECK(state!='DONE' OR result_receipt_ref_json IS NOT NULL)
) STRICT;
CREATE INDEX arp_jobs_due ON arp_jobs(state,next_at_ms);
CREATE TABLE arp_blob_roots (
 root_key TEXT PRIMARY KEY NOT NULL,session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id),
 object_ref_json TEXT NOT NULL CHECK(json_valid(object_ref_json)),
 purpose TEXT NOT NULL CHECK(purpose IN ('REQUEST','SKILL_USE','TRANSFER','AUDIT')),
 owner_ref_json TEXT NOT NULL CHECK(json_valid(owner_ref_json)),
 state TEXT NOT NULL CHECK(state IN ('LIVE','TRANSFERRED','RELEASED')),
 transfer_receipt_ref_json TEXT CHECK(transfer_receipt_ref_json IS NULL OR json_valid(transfer_receipt_ref_json)),
 row_version INTEGER NOT NULL CHECK(row_version>0),
 CHECK(state!='TRANSFERRED' OR transfer_receipt_ref_json IS NOT NULL)
) STRICT;
CREATE TRIGGER arp_profiles_update_deny BEFORE UPDATE ON arp_profiles BEGIN SELECT RAISE(ABORT,'immutable arp_profiles'); END;
CREATE TRIGGER arp_profiles_delete_deny BEFORE DELETE ON arp_profiles
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_profiles' AND p.row_key=json_array(OLD.profile_id,OLD.revision) AND p.body_hash=OLD.body_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_context_requests_update_deny BEFORE UPDATE ON arp_context_requests BEGIN SELECT RAISE(ABORT,'immutable arp_context_requests'); END;
CREATE TRIGGER arp_context_requests_delete_deny BEFORE DELETE ON arp_context_requests
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_context_requests' AND p.row_key=json_array(OLD.context_id) AND p.body_hash=OLD.manifest_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_catalog_revisions_update_deny BEFORE UPDATE ON arp_catalog_revisions BEGIN SELECT RAISE(ABORT,'immutable arp_catalog_revisions'); END;
CREATE TRIGGER arp_catalog_revisions_delete_deny BEFORE DELETE ON arp_catalog_revisions
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_catalog_revisions' AND p.row_key=json_array(OLD.namespace_id,OLD.entry_kind,OLD.entry_id,OLD.revision) AND p.body_hash=OLD.content_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_capability_bindings_update_deny BEFORE UPDATE ON arp_capability_bindings BEGIN SELECT RAISE(ABORT,'immutable arp_capability_bindings'); END;
CREATE TRIGGER arp_capability_bindings_delete_deny BEFORE DELETE ON arp_capability_bindings
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_capability_bindings' AND p.row_key=json_array(OLD.binding_id) AND p.body_hash=OLD.binding_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_tool_exposures_update_deny BEFORE UPDATE ON arp_tool_exposures BEGIN SELECT RAISE(ABORT,'immutable arp_tool_exposures'); END;
CREATE TRIGGER arp_tool_exposures_delete_deny BEFORE DELETE ON arp_tool_exposures
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_tool_exposures' AND p.row_key=json_array(OLD.snapshot_id) AND p.body_hash=OLD.snapshot_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_skill_uses_update_deny BEFORE UPDATE ON arp_skill_uses BEGIN SELECT RAISE(ABORT,'immutable arp_skill_uses'); END;
CREATE TRIGGER arp_skill_uses_delete_deny BEFORE DELETE ON arp_skill_uses
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_skill_uses' AND p.row_key=json_array(OLD.use_id) AND p.body_hash=OLD.use_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;

CREATE TRIGGER arp_session_initial BEFORE INSERT ON arp_agent_sessions
WHEN NEW.state!='CREATING' OR NEW.generation!=1 OR NEW.row_version!=1 OR NEW.destroy_command_id IS NOT NULL OR NEW.purge_progress_json IS NOT NULL
BEGIN SELECT RAISE(ABORT,'session must start CREATING'); END;
CREATE TRIGGER arp_session_transition BEFORE UPDATE ON arp_agent_sessions
WHEN NEW.row_version!=OLD.row_version+1 OR NEW.generation<OLD.generation
 OR NEW.session_id!=OLD.session_id OR NEW.agent_id!=OLD.agent_id OR NEW.creation_key!=OLD.creation_key
 OR NEW.profile_hash!=OLD.profile_hash OR NEW.creation_root_id!=OLD.creation_root_id
 OR NEW.root_incarnation!=OLD.root_incarnation OR NEW.relative_directory!=OLD.relative_directory
 OR NEW.journal_seq_from!=OLD.journal_seq_from
 OR OLD.state='PURGED'
 OR (OLD.destroy_command_id IS NOT NULL AND (NEW.destroy_command_id IS NOT OLD.destroy_command_id OR NEW.destroy_command_hash IS NOT OLD.destroy_command_hash OR NEW.generation!=OLD.generation OR NEW.state IN ('ACTIVE','CREATING','QUARANTINED')))
 OR (NEW.state IN ('ACTIVE','CREATING','QUARANTINED') AND NEW.destroy_command_id IS NOT NULL)
 OR (NEW.state!=OLD.state AND NOT (
  (OLD.state='CREATING' AND NEW.state IN ('ACTIVE','DRAINING','QUARANTINED')) OR
  (OLD.state='ACTIVE' AND NEW.state IN ('DRAINING','QUARANTINED')) OR
  (OLD.state='QUARANTINED' AND NEW.state IN ('ACTIVE','DRAINING')) OR
  (OLD.state='DRAINING' AND NEW.state='PURGING') OR
  (OLD.state='PURGING' AND NEW.state='PURGED')))
 OR (NEW.state IN ('DRAINING','QUARANTINED') AND NEW.state!=OLD.state AND NEW.generation!=OLD.generation+1)
BEGIN SELECT RAISE(ABORT,'invalid session change'); END;
CREATE TRIGGER arp_session_no_delete BEFORE DELETE ON arp_agent_sessions
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_agent_sessions' AND p.row_key=json_array(OLD.session_id) AND p.body_hash=OLD.create_command_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_context_live_insert BEFORE INSERT ON arp_context_requests
WHEN NOT EXISTS(SELECT 1 FROM arp_agent_sessions s WHERE s.session_id=NEW.session_id
 AND s.agent_id=NEW.agent_id AND s.state='ACTIVE' AND s.generation=NEW.session_generation)
 OR NOT EXISTS(SELECT 1 FROM base_agent_turns_v1 t WHERE t.turn_id=NEW.turn_id AND t.agent_id=NEW.agent_id)
BEGIN SELECT RAISE(ABORT,'stale/wrong session or turn'); END;
CREATE TRIGGER arp_activation_initial BEFORE INSERT ON arp_catalog_activation
WHEN NEW.state!='QUARANTINED' OR NEW.row_version!=1
BEGIN SELECT RAISE(ABORT,'catalogue starts quarantined'); END;
CREATE TRIGGER arp_activation_transition BEFORE UPDATE ON arp_catalog_activation
WHEN NEW.row_version!=OLD.row_version+1 OR OLD.state='RETIRED'
 OR NEW.namespace_id!=OLD.namespace_id OR NEW.entry_kind!=OLD.entry_kind OR NEW.entry_id!=OLD.entry_id OR NEW.revision!=OLD.revision OR NEW.content_hash!=OLD.content_hash
 OR (NEW.state!=OLD.state AND NOT (
 (OLD.state='QUARANTINED' AND NEW.state IN ('TRIAL','RETIRED')) OR
 (OLD.state='TRIAL' AND NEW.state IN ('ADMITTED','SUSPENDED','RETIRED')) OR
 (OLD.state='ADMITTED' AND NEW.state IN ('SUSPENDED','RETIRED')) OR
 (OLD.state='SUSPENDED' AND NEW.state IN ('TRIAL','ADMITTED','RETIRED'))))
 OR (NEW.entry_kind='SKILL' AND NEW.state='ADMITTED' AND NEW.evaluation_ref_json IS NULL)
BEGIN SELECT RAISE(ABORT,'invalid catalogue lifecycle'); END;
CREATE TRIGGER arp_activation_no_delete BEFORE DELETE ON arp_catalog_activation
BEGIN SELECT RAISE(ABORT,'catalogue lifecycle retained'); END;
CREATE TRIGGER arp_registry_monotone BEFORE UPDATE ON arp_registry_epoch
WHEN NEW.namespace_id!=OLD.namespace_id OR NEW.epoch!=OLD.epoch+1
BEGIN SELECT RAISE(ABORT,'epoch must advance exactly once'); END;
CREATE TRIGGER arp_registry_no_delete BEFORE DELETE ON arp_registry_epoch
BEGIN SELECT RAISE(ABORT,'registry epoch retained'); END;
CREATE TRIGGER arp_job_initial BEFORE INSERT ON arp_jobs
WHEN NEW.state!='PENDING' OR NEW.row_version!=1 OR NEW.attempts!=0
BEGIN SELECT RAISE(ABORT,'job starts pending'); END;
CREATE TRIGGER arp_job_transition BEFORE UPDATE ON arp_jobs
WHEN NEW.row_version!=OLD.row_version+1 OR NEW.attempts<OLD.attempts
 OR OLD.state IN ('DONE','CANCELLED')
 OR NEW.session_id!=OLD.session_id OR NEW.kind!=OLD.kind OR NEW.semantic_key!=OLD.semantic_key
 OR NEW.payload_hash!=OLD.payload_hash OR NEW.generation!=OLD.generation
 OR (NEW.state!=OLD.state AND NOT (
 (OLD.state='PENDING' AND NEW.state IN ('LEASED','BLOCKED','CANCELLED')) OR
 (OLD.state='LEASED' AND NEW.state IN ('PENDING','DONE','BLOCKED','CANCELLED')) OR
 (OLD.state='BLOCKED' AND NEW.state IN ('PENDING','CANCELLED'))))
BEGIN SELECT RAISE(ABORT,'invalid job state'); END;
CREATE TRIGGER arp_job_no_delete BEFORE DELETE ON arp_jobs
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_jobs' AND p.row_key=json_array(OLD.job_id) AND p.body_hash=OLD.payload_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
CREATE TRIGGER arp_pin_initial BEFORE INSERT ON arp_blob_roots
WHEN NEW.state!='LIVE' OR NEW.row_version!=1
BEGIN SELECT RAISE(ABORT,'root starts live'); END;
CREATE TRIGGER arp_pin_transition BEFORE UPDATE ON arp_blob_roots
WHEN NEW.row_version!=OLD.row_version+1 OR OLD.state!='LIVE'
 OR NEW.state NOT IN ('TRANSFERRED','RELEASED') OR NEW.object_ref_json!=OLD.object_ref_json
 OR NEW.owner_ref_json!=OLD.owner_ref_json OR NEW.session_id!=OLD.session_id
BEGIN SELECT RAISE(ABORT,'invalid root release'); END;
CREATE TRIGGER arp_pin_no_delete BEFORE DELETE ON arp_blob_roots
BEGIN SELECT RAISE(ABORT,'root receipt retained'); END;

CREATE TRIGGER arp_protocol_update_deny BEFORE UPDATE ON arp_agent_protocols
BEGIN SELECT RAISE(ABORT,'immutable creation protocol'); END;
CREATE TRIGGER arp_protocol_delete_deny BEFORE DELETE ON arp_agent_protocols
BEGIN SELECT RAISE(ABORT,'creation protocol retained'); END;
CREATE TRIGGER arp_session_protocol BEFORE INSERT ON arp_agent_sessions
WHEN NOT EXISTS(SELECT 1 FROM arp_agent_protocols p WHERE p.agent_id=NEW.agent_id AND p.protocol IN ('ARP_V1','ARP_V1_1','ARP_V1_1_1'))
BEGIN SELECT RAISE(ABORT,'explicit ARP creation protocol required'); END;
CREATE TRIGGER arp_policy_adoption_sequence BEFORE INSERT ON arp_context_policy_adoptions
WHEN NEW.adoption_revision != COALESCE((SELECT MAX(adoption_revision)+1 FROM arp_context_policy_adoptions WHERE session_id=NEW.session_id),1)
 OR NOT EXISTS(SELECT 1 FROM arp_agent_sessions s WHERE s.session_id=NEW.session_id AND s.state IN ('CREATING','ACTIVE'))
BEGIN SELECT RAISE(ABORT,'policy adoption sequence/session state'); END;
CREATE TRIGGER arp_policy_adoption_update_deny BEFORE UPDATE ON arp_context_policy_adoptions
BEGIN SELECT RAISE(ABORT,'immutable policy adoption'); END;
CREATE TRIGGER arp_policy_adoption_delete_deny BEFORE DELETE ON arp_context_policy_adoptions
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_context_policy_adoptions' AND p.row_key=json_array(OLD.session_id,OLD.adoption_revision) AND p.body_hash=OLD.policy_body_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;

CREATE TRIGGER arp_session_identity BEFORE UPDATE ON arp_agent_sessions
WHEN NEW.session_id IS NOT OLD.session_id OR NEW.agent_id IS NOT OLD.agent_id
 OR NEW.profile_id IS NOT OLD.profile_id OR NEW.profile_revision IS NOT OLD.profile_revision OR NEW.profile_hash IS NOT OLD.profile_hash
 OR NEW.creation_root_id IS NOT OLD.creation_root_id OR NEW.root_incarnation IS NOT OLD.root_incarnation
 OR NEW.creation_key IS NOT OLD.creation_key OR NEW.create_command_hash IS NOT OLD.create_command_hash
 OR NEW.relative_directory IS NOT OLD.relative_directory OR NEW.journal_seq_from IS NOT OLD.journal_seq_from
 OR NEW.created_at_ms IS NOT OLD.created_at_ms
 OR (OLD.destroy_command_id IS NOT NULL AND (NEW.destroy_command_id IS NOT OLD.destroy_command_id OR NEW.destroy_command_hash IS NOT OLD.destroy_command_hash))
 OR (OLD.sealed_highwater IS NOT NULL AND NEW.sealed_highwater IS NOT OLD.sealed_highwater)
 OR (NEW.state=OLD.state AND NEW.generation!=OLD.generation)
BEGIN SELECT RAISE(ABORT,'session immutable identity/fence'); END;


-- New narrow binding records. No call status/charges duplicated here.
CREATE TABLE arp_creation_intents (
 intent_id TEXT PRIMARY KEY NOT NULL, owner_scope TEXT NOT NULL, creation_key TEXT NOT NULL,
 command_hash TEXT NOT NULL CHECK(length(command_hash)=64), proposed_agent_id TEXT NOT NULL UNIQUE,
 proposed_run_id TEXT NOT NULL UNIQUE, profile_ref_json TEXT NOT NULL CHECK(json_valid(profile_ref_json)),
 state TEXT NOT NULL CHECK(state IN ('PREPARED','BOUND','ABORTED')),
 row_version INTEGER NOT NULL CHECK(row_version>0), original_receipt_ref_json TEXT NOT NULL CHECK(json_valid(original_receipt_ref_json)),
 UNIQUE(owner_scope,creation_key)
) STRICT;
CREATE TABLE arp_policy_objects (
 policy_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 body_json TEXT NOT NULL CHECK(json_valid(body_json)), approval_ref_json TEXT NOT NULL CHECK(json_valid(approval_ref_json)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)), PRIMARY KEY(policy_id,revision)
) STRICT;
CREATE TABLE arp_index_publications (
 session_id TEXT NOT NULL REFERENCES arp_agent_sessions(session_id), index_generation INTEGER NOT NULL CHECK(index_generation>0),
 partition_id TEXT NOT NULL, generation_manifest_hash TEXT NOT NULL CHECK(length(generation_manifest_hash)=64),
 source_highwater INTEGER NOT NULL CHECK(source_highwater>=0), state TEXT NOT NULL CHECK(state IN ('ACTIVE','RETIRED')),
 publish_receipt_ref_json TEXT NOT NULL CHECK(json_valid(publish_receipt_ref_json)), PRIMARY KEY(session_id,index_generation)
) STRICT;
CREATE UNIQUE INDEX arp_single_active_index ON arp_index_publications(session_id) WHERE state='ACTIVE';
CREATE TABLE arp_catalogue_mounts (
 namespace_id TEXT PRIMARY KEY NOT NULL, catalogue_owner_root_id TEXT NOT NULL, realm_scope_ref_json TEXT NOT NULL CHECK(json_valid(realm_scope_ref_json)),
 mount_receipt_ref_json TEXT NOT NULL CHECK(json_valid(mount_receipt_ref_json))
) STRICT;
CREATE TABLE arp_dependency_locks (
 skill_id TEXT NOT NULL, skill_revision INTEGER NOT NULL, lock_hash TEXT NOT NULL CHECK(length(lock_hash)=64),
 lock_json TEXT NOT NULL CHECK(json_valid(lock_json)), complete INTEGER NOT NULL CHECK(complete IN (0,1)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)), PRIMARY KEY(skill_id,skill_revision,lock_hash)
) STRICT;
-- Skill import command journal (§9.4): one command id, one bundle, one outcome.
CREATE TABLE arp_skill_import_commands (
 namespace_id TEXT NOT NULL, command_id TEXT NOT NULL, bundle_hash TEXT NOT NULL CHECK(length(bundle_hash)=64),
 skill_id TEXT NOT NULL, skill_revision INTEGER NOT NULL CHECK(skill_revision>0), PRIMARY KEY(namespace_id,command_id)
) STRICT;
CREATE TRIGGER arp_skill_import_commands_update_deny BEFORE UPDATE ON arp_skill_import_commands BEGIN SELECT RAISE(ABORT,'immutable skill import command'); END;
-- Skill trial bindings (§9.6): one immutable SkillEvaluationBinding per trial command.
CREATE TABLE arp_skill_evaluations (
 namespace_id TEXT NOT NULL, evaluation_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64), command_id TEXT NOT NULL,
 skill_id TEXT NOT NULL, skill_revision INTEGER NOT NULL CHECK(skill_revision>0), lock_hash TEXT NOT NULL CHECK(length(lock_hash)=64),
 body_json TEXT NOT NULL CHECK(json_valid(body_json)), expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=0),
 PRIMARY KEY(namespace_id,evaluation_id,revision), UNIQUE(namespace_id,command_id)
) STRICT;
CREATE TRIGGER arp_skill_evaluations_update_deny BEFORE UPDATE ON arp_skill_evaluations BEGIN SELECT RAISE(ABORT,'immutable skill evaluation binding'); END;
-- Skill admissions (§9.7): which official acceptance admitted which evaluation; immutable.
CREATE TABLE arp_skill_admissions (
 namespace_id TEXT NOT NULL, skill_id TEXT NOT NULL, skill_revision INTEGER NOT NULL CHECK(skill_revision>0),
 evaluation_id TEXT NOT NULL, acceptance_ref_json TEXT NOT NULL CHECK(json_valid(acceptance_ref_json)), command_id TEXT NOT NULL,
 PRIMARY KEY(namespace_id,skill_id,skill_revision,evaluation_id)
) STRICT;
CREATE TRIGGER arp_skill_admissions_update_deny BEFORE UPDATE ON arp_skill_admissions BEGIN SELECT RAISE(ABORT,'immutable skill admission'); END;
CREATE TABLE arp_event_bindings (
 original_event_id TEXT PRIMARY KEY NOT NULL, original_eventseq INTEGER NOT NULL CHECK(original_eventseq>=0),
 event_type TEXT NOT NULL CHECK(event_type IN ('AgentContextPolicyAdopted','RuntimeContextPrepared','RuntimeContextExposed','RuntimeSessionStateChanged','RuntimeIndexGenerationPublished','RuntimeJobChanged','RuntimeCatalogueChanged')),
 body_hash TEXT NOT NULL CHECK(length(body_hash)=64), body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json))
) STRICT;
CREATE TABLE arp_retention_permits (
 table_name TEXT NOT NULL, row_key TEXT NOT NULL, body_hash TEXT NOT NULL CHECK(length(body_hash)=64),
 expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=0), source_receipt_ref_json TEXT NOT NULL CHECK(json_valid(source_receipt_ref_json)),
 PRIMARY KEY(table_name,row_key,body_hash)
) STRICT;
CREATE TRIGGER arp_creation_initial BEFORE INSERT ON arp_creation_intents WHEN NEW.state!='PREPARED' OR NEW.row_version!=1
BEGIN SELECT RAISE(ABORT,'creation intent must start prepared'); END;
CREATE TRIGGER arp_creation_transition BEFORE UPDATE ON arp_creation_intents
WHEN OLD.state!='PREPARED' OR NEW.state NOT IN ('BOUND','ABORTED') OR NEW.row_version!=OLD.row_version+1
 OR NEW.intent_id!=OLD.intent_id OR NEW.command_hash!=OLD.command_hash OR NEW.proposed_agent_id!=OLD.proposed_agent_id OR NEW.proposed_run_id!=OLD.proposed_run_id OR NEW.profile_ref_json!=OLD.profile_ref_json
BEGIN SELECT RAISE(ABORT,'creation identity/transition'); END;
CREATE TRIGGER arp_index_publication_transition BEFORE UPDATE ON arp_index_publications
WHEN OLD.state!='ACTIVE' OR NEW.state!='RETIRED' OR NEW.session_id!=OLD.session_id OR NEW.index_generation!=OLD.index_generation OR NEW.partition_id!=OLD.partition_id OR NEW.generation_manifest_hash!=OLD.generation_manifest_hash
BEGIN SELECT RAISE(ABORT,'index publication immutable identity'); END;

CREATE TRIGGER arp_profiles_no_replace BEFORE INSERT ON arp_profiles WHEN EXISTS(SELECT 1 FROM arp_profiles o WHERE o.profile_id=NEW.profile_id AND o.revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_agent_protocols_no_replace BEFORE INSERT ON arp_agent_protocols WHEN EXISTS(SELECT 1 FROM arp_agent_protocols o WHERE o.agent_id=NEW.agent_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_agent_sessions_no_replace BEFORE INSERT ON arp_agent_sessions WHEN EXISTS(SELECT 1 FROM arp_agent_sessions o WHERE o.session_id=NEW.session_id) OR EXISTS(SELECT 1 FROM arp_agent_sessions o WHERE o.creation_key=NEW.creation_key)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_context_policy_adoptions_no_replace BEFORE INSERT ON arp_context_policy_adoptions WHEN EXISTS(SELECT 1 FROM arp_context_policy_adoptions o WHERE o.session_id=NEW.session_id AND o.adoption_revision=NEW.adoption_revision) OR EXISTS(SELECT 1 FROM arp_context_policy_adoptions o WHERE o.command_id=NEW.command_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_context_requests_no_replace BEFORE INSERT ON arp_context_requests WHEN EXISTS(SELECT 1 FROM arp_context_requests o WHERE o.context_id=NEW.context_id) OR EXISTS(SELECT 1 FROM arp_context_requests o WHERE o.original_request_key=NEW.original_request_key) OR EXISTS(SELECT 1 FROM arp_context_requests o WHERE o.turn_id=NEW.turn_id AND o.provider_request_ordinal=NEW.provider_request_ordinal)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_catalog_revisions_no_replace BEFORE INSERT ON arp_catalog_revisions WHEN EXISTS(SELECT 1 FROM arp_catalog_revisions o WHERE o.namespace_id=NEW.namespace_id AND o.entry_kind=NEW.entry_kind AND o.entry_id=NEW.entry_id AND o.revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_catalog_activation_no_replace BEFORE INSERT ON arp_catalog_activation WHEN EXISTS(SELECT 1 FROM arp_catalog_activation o WHERE o.namespace_id=NEW.namespace_id AND o.entry_kind=NEW.entry_kind AND o.entry_id=NEW.entry_id AND o.revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_registry_epoch_no_replace BEFORE INSERT ON arp_registry_epoch WHEN EXISTS(SELECT 1 FROM arp_registry_epoch o WHERE o.namespace_id=NEW.namespace_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_skill_uses_no_replace BEFORE INSERT ON arp_skill_uses WHEN EXISTS(SELECT 1 FROM arp_skill_uses o WHERE o.use_id=NEW.use_id) OR EXISTS(SELECT 1 FROM arp_skill_uses o WHERE o.session_id=NEW.session_id AND o.use_key=NEW.use_key)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_capability_bindings_no_replace BEFORE INSERT ON arp_capability_bindings WHEN EXISTS(SELECT 1 FROM arp_capability_bindings o WHERE o.binding_id=NEW.binding_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_tool_exposures_no_replace BEFORE INSERT ON arp_tool_exposures WHEN EXISTS(SELECT 1 FROM arp_tool_exposures o WHERE o.snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_jobs_no_replace BEFORE INSERT ON arp_jobs WHEN EXISTS(SELECT 1 FROM arp_jobs o WHERE o.job_id=NEW.job_id) OR EXISTS(SELECT 1 FROM arp_jobs o WHERE o.session_id=NEW.session_id AND o.kind=NEW.kind AND o.semantic_key=NEW.semantic_key)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_blob_roots_no_replace BEFORE INSERT ON arp_blob_roots WHEN EXISTS(SELECT 1 FROM arp_blob_roots o WHERE o.root_key=NEW.root_key)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_creation_intents_no_replace BEFORE INSERT ON arp_creation_intents WHEN EXISTS(SELECT 1 FROM arp_creation_intents o WHERE o.intent_id=NEW.intent_id) OR EXISTS(SELECT 1 FROM arp_creation_intents o WHERE o.owner_scope=NEW.owner_scope AND o.creation_key=NEW.creation_key) OR EXISTS(SELECT 1 FROM arp_creation_intents o WHERE o.proposed_agent_id=NEW.proposed_agent_id) OR EXISTS(SELECT 1 FROM arp_creation_intents o WHERE o.proposed_run_id=NEW.proposed_run_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_policy_objects_no_replace BEFORE INSERT ON arp_policy_objects WHEN EXISTS(SELECT 1 FROM arp_policy_objects o WHERE o.policy_id=NEW.policy_id AND o.revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_index_publications_no_replace BEFORE INSERT ON arp_index_publications WHEN EXISTS(SELECT 1 FROM arp_index_publications o WHERE o.session_id=NEW.session_id AND o.index_generation=NEW.index_generation)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_catalogue_mounts_no_replace BEFORE INSERT ON arp_catalogue_mounts WHEN EXISTS(SELECT 1 FROM arp_catalogue_mounts o WHERE o.namespace_id=NEW.namespace_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_dependency_locks_no_replace BEFORE INSERT ON arp_dependency_locks WHEN EXISTS(SELECT 1 FROM arp_dependency_locks o WHERE o.skill_id=NEW.skill_id AND o.skill_revision=NEW.skill_revision AND o.lock_hash=NEW.lock_hash)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_event_bindings_no_replace BEFORE INSERT ON arp_event_bindings WHEN EXISTS(SELECT 1 FROM arp_event_bindings o WHERE o.original_event_id=NEW.original_event_id)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;

CREATE TRIGGER arp_retention_permits_no_replace BEFORE INSERT ON arp_retention_permits WHEN EXISTS(SELECT 1 FROM arp_retention_permits o WHERE o.table_name=NEW.table_name AND o.row_key=NEW.row_key AND o.body_hash=NEW.body_hash)
BEGIN SELECT RAISE(ABORT,'identity already exists; compare at Store, never REPLACE'); END;
CREATE TRIGGER arp_policy_object_update_deny BEFORE UPDATE ON arp_policy_objects BEGIN SELECT RAISE(ABORT,'immutable policy object'); END;
CREATE TRIGGER arp_dependency_lock_update_deny BEFORE UPDATE ON arp_dependency_locks BEGIN SELECT RAISE(ABORT,'immutable dependency lock'); END;
CREATE TRIGGER arp_event_binding_update_deny BEFORE UPDATE ON arp_event_bindings BEGIN SELECT RAISE(ABORT,'immutable original event binding'); END;
CREATE TRIGGER arp_catalog_mount_update_deny BEFORE UPDATE ON arp_catalogue_mounts BEGIN SELECT RAISE(ABORT,'catalogue realm owner immutable; approved new mount lifecycle required'); END;
CREATE TRIGGER arp_creation_frozen_fields BEFORE UPDATE ON arp_creation_intents
WHEN NEW.owner_scope IS NOT OLD.owner_scope OR NEW.creation_key IS NOT OLD.creation_key OR NEW.original_receipt_ref_json IS NOT OLD.original_receipt_ref_json
BEGIN SELECT RAISE(ABORT,'creation source binding immutable'); END;
CREATE TRIGGER arp_publication_frozen_fields BEFORE UPDATE ON arp_index_publications
WHEN NEW.source_highwater IS NOT OLD.source_highwater OR NEW.publish_receipt_ref_json IS NOT OLD.publish_receipt_ref_json
BEGIN SELECT RAISE(ABORT,'publication source immutable'); END;

-- Projection of original health probe receipts, not an invocation/effect ledger.
CREATE TABLE arp_deployment_health (
 namespace_id TEXT NOT NULL, deployment_id TEXT NOT NULL, definition_revision INTEGER NOT NULL CHECK(definition_revision>0),
 health_revision INTEGER NOT NULL CHECK(health_revision>0), snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
 body_json TEXT NOT NULL CHECK(json_valid(body_json)), probe_receipt_ref_json TEXT NOT NULL CHECK(json_valid(probe_receipt_ref_json)),
 PRIMARY KEY(namespace_id,deployment_id,definition_revision,health_revision)
) STRICT;
CREATE TRIGGER arp_health_update_deny BEFORE UPDATE ON arp_deployment_health BEGIN SELECT RAISE(ABORT,'health snapshot immutable'); END;
CREATE TRIGGER arp_health_no_replace BEFORE INSERT ON arp_deployment_health WHEN EXISTS(SELECT 1 FROM arp_deployment_health WHERE namespace_id=NEW.namespace_id AND deployment_id=NEW.deployment_id AND definition_revision=NEW.definition_revision AND health_revision=NEW.health_revision)
BEGIN SELECT RAISE(ABORT,'health observation identity conflict'); END;

-- R1: deletion isolation stays in DRAINING/PURGING; never returns to QUARANTINED.
CREATE TRIGGER arp_purge_progress_shape BEFORE UPDATE ON arp_agent_sessions
WHEN (NEW.state IN ('DRAINING','PURGING','PURGED') AND (
 NEW.destroy_command_id IS NULL OR NEW.purge_progress_json IS NULL
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.schema_version')=1,0)=0
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.session_id')=NEW.session_id,0)=0
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.agent_id')=NEW.agent_id,0)=0
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.root_incarnation')=NEW.root_incarnation,0)=0
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.destroy_command_id')=NEW.destroy_command_id,0)=0
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.destroy_command_hash')=NEW.destroy_command_hash,0)=0
 OR COALESCE(json_extract(NEW.purge_progress_json,'$.control_generation')=NEW.generation,0)=0
 OR COALESCE(json_type(NEW.purge_progress_json,'$.phase')='text',0)=0
 OR (NEW.state='DRAINING' AND json_extract(NEW.purge_progress_json,'$.phase')!='DRAINING')
 OR (NEW.state='PURGING' AND json_extract(NEW.purge_progress_json,'$.phase') NOT IN ('RENAME_PENDING','RENAMED','DELETE_CONFIRMED'))
 OR (NEW.state='PURGED' AND (json_extract(NEW.purge_progress_json,'$.phase')!='DELETE_CONFIRMED'
     OR COALESCE(json_type(NEW.purge_progress_json,'$.delete_receipt_ref')='object',0)=0
     OR COALESCE(json_type(NEW.purge_progress_json,'$.blocking')='null',0)=0))
 )) OR (NEW.state NOT IN ('DRAINING','PURGING','PURGED') AND NEW.purge_progress_json IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'invalid purge progress'); END;
CREATE TRIGGER arp_purge_progress_identity BEFORE UPDATE ON arp_agent_sessions
WHEN OLD.purge_progress_json IS NOT NULL AND (
 NEW.purge_progress_json IS NULL
 OR json_extract(NEW.purge_progress_json,'$.source_relative_directory') IS NOT json_extract(OLD.purge_progress_json,'$.source_relative_directory')
 OR json_extract(NEW.purge_progress_json,'$.trash_relative_directory') IS NOT json_extract(OLD.purge_progress_json,'$.trash_relative_directory')
 OR json_extract(NEW.purge_progress_json,'$.expected_marker_hash') IS NOT json_extract(OLD.purge_progress_json,'$.expected_marker_hash')
 OR json_extract(NEW.purge_progress_json,'$.destroy_receipt_ref') IS NOT json_extract(OLD.purge_progress_json,'$.destroy_receipt_ref')
 OR (OLD.state='DRAINING' AND NEW.state='PURGING' AND json_extract(NEW.purge_progress_json,'$.phase') IS NOT 'RENAME_PENDING')
 OR (json_type(OLD.purge_progress_json,'$.rename_receipt_ref')='object' AND json_extract(NEW.purge_progress_json,'$.rename_receipt_ref') IS NOT json_extract(OLD.purge_progress_json,'$.rename_receipt_ref'))
 OR (json_type(OLD.purge_progress_json,'$.delete_receipt_ref')='object' AND json_extract(NEW.purge_progress_json,'$.delete_receipt_ref') IS NOT json_extract(OLD.purge_progress_json,'$.delete_receipt_ref'))
 OR (OLD.state='PURGING' AND NOT (
  json_extract(NEW.purge_progress_json,'$.phase')=json_extract(OLD.purge_progress_json,'$.phase')
  OR (json_extract(OLD.purge_progress_json,'$.phase')='RENAME_PENDING' AND json_extract(NEW.purge_progress_json,'$.phase')='RENAMED')
  OR (json_extract(OLD.purge_progress_json,'$.phase')='RENAMED' AND json_extract(NEW.purge_progress_json,'$.phase')='DELETE_CONFIRMED')))
 )
BEGIN SELECT RAISE(ABORT,'purge identity/phase cannot change'); END;

-- R3: coordination only; actual query embedding call/usage remain in original ledger.
CREATE TABLE arp_context_recalls (
 recall_key TEXT PRIMARY KEY NOT NULL,
 session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 turn_id TEXT NOT NULL REFERENCES base_agent_turns_v1(turn_id),
 original_request_key TEXT NOT NULL UNIQUE,
 provider_request_ordinal INTEGER NOT NULL CHECK(provider_request_ordinal>=0),
 control_generation INTEGER NOT NULL CHECK(control_generation>0),
 request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 request_json TEXT NOT NULL CHECK(json_valid(request_json)),
 query_id TEXT NOT NULL UNIQUE,
 index_snapshot_ref_json TEXT CHECK(index_snapshot_ref_json IS NULL OR json_valid(index_snapshot_ref_json)),
 embedding_invocation_ref_json TEXT CHECK(embedding_invocation_ref_json IS NULL OR json_valid(embedding_invocation_ref_json)),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARING','WAITING_EMBEDDING','SCANNING','FETCHING_RESULTS','READY','SKIPPED','BLOCKED','STALE')),
 progress_json TEXT NOT NULL CHECK(json_valid(progress_json)),
 result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
 result_hash TEXT CHECK(result_hash IS NULL OR length(result_hash)=64),
 deadline_ms INTEGER NOT NULL CHECK(deadline_ms>=0),next_wake_at_ms INTEGER NOT NULL CHECK(next_wake_at_ms>=0),
 row_version INTEGER NOT NULL CHECK(row_version>0),
 UNIQUE(turn_id,provider_request_ordinal),
 FOREIGN KEY(session_id,agent_id) REFERENCES arp_agent_sessions(session_id,agent_id),
 CHECK((result_json IS NULL)=(result_hash IS NULL)),
 CHECK((phase IN ('READY','SKIPPED'))=(result_json IS NOT NULL))
) STRICT;
CREATE TRIGGER arp_recall_initial BEFORE INSERT ON arp_context_recalls
WHEN NEW.phase!='PREPARING' OR NEW.row_version!=1 OR NEW.result_json IS NOT NULL
 OR NOT EXISTS(SELECT 1 FROM arp_agent_sessions s WHERE s.session_id=NEW.session_id AND s.agent_id=NEW.agent_id AND s.state='ACTIVE' AND s.generation=NEW.control_generation)
 OR NOT EXISTS(SELECT 1 FROM base_agent_turns_v1 t WHERE t.turn_id=NEW.turn_id AND t.agent_id=NEW.agent_id)
BEGIN SELECT RAISE(ABORT,'invalid recall creation'); END;
CREATE TRIGGER arp_recall_transition BEFORE UPDATE ON arp_context_recalls
WHEN NEW.row_version!=OLD.row_version+1 OR OLD.phase IN ('READY','SKIPPED','BLOCKED','STALE')
 OR NEW.recall_key!=OLD.recall_key OR NEW.original_request_key!=OLD.original_request_key
 OR NEW.agent_id!=OLD.agent_id OR NEW.session_id!=OLD.session_id OR NEW.turn_id!=OLD.turn_id
 OR NEW.provider_request_ordinal!=OLD.provider_request_ordinal OR NEW.control_generation!=OLD.control_generation
 OR NEW.request_hash!=OLD.request_hash OR NEW.request_json!=OLD.request_json OR NEW.query_id!=OLD.query_id
 OR NEW.deadline_ms!=OLD.deadline_ms
 OR (OLD.embedding_invocation_ref_json IS NOT NULL AND NEW.embedding_invocation_ref_json IS NOT OLD.embedding_invocation_ref_json)
 OR (OLD.index_snapshot_ref_json IS NOT NULL AND NEW.index_snapshot_ref_json IS NOT OLD.index_snapshot_ref_json)
 OR (NEW.phase!=OLD.phase AND NOT (
   (NEW.phase IN ('SKIPPED','BLOCKED','STALE'))
   OR (OLD.phase='PREPARING' AND NEW.phase IN ('WAITING_EMBEDDING','SCANNING'))
   OR (OLD.phase='WAITING_EMBEDDING' AND NEW.phase='SCANNING')
   OR (OLD.phase='SCANNING' AND NEW.phase='FETCHING_RESULTS')
   OR (OLD.phase='FETCHING_RESULTS' AND NEW.phase='READY')))
BEGIN SELECT RAISE(ABORT,'recall identity/state conflict'); END;
CREATE TRIGGER arp_recall_no_replace BEFORE INSERT ON arp_context_recalls
WHEN EXISTS(SELECT 1 FROM arp_context_recalls WHERE recall_key=NEW.recall_key OR original_request_key=NEW.original_request_key OR (turn_id=NEW.turn_id AND provider_request_ordinal=NEW.provider_request_ordinal))
BEGIN SELECT RAISE(ABORT,'recall replay must read existing identity'); END;
CREATE TRIGGER arp_recall_no_delete BEFORE DELETE ON arp_context_recalls
WHEN NOT EXISTS(SELECT 1 FROM arp_retention_permits p WHERE p.table_name='arp_context_recalls' AND p.row_key=json_array(OLD.recall_key) AND p.body_hash=OLD.request_hash AND p.expires_at_ms>=CAST(strftime('%s','now') AS INTEGER)*1000)
BEGIN SELECT RAISE(ABORT,'retention permit required'); END;
