-- ARP native partition schema v2. Derived data only; original Journal immutable.
-- One physical DB per logical session. Index generation != session control generation.
CREATE TABLE session_partition (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),session_id TEXT NOT NULL,agent_id TEXT NOT NULL,
 root_id TEXT NOT NULL,root_incarnation TEXT NOT NULL,partition_id TEXT NOT NULL,
 control_generation INTEGER NOT NULL CHECK(control_generation>0),
 source_from INTEGER NOT NULL CHECK(source_from>0), schema_version INTEGER NOT NULL CHECK(schema_version=2),
 marker_hash TEXT NOT NULL CHECK(length(marker_hash)=64)
) STRICT;
CREATE TABLE index_generations (
 index_generation INTEGER PRIMARY KEY CHECK(index_generation>0),
 view_policy_hash TEXT NOT NULL CHECK(length(view_policy_hash)=64),
 chunker_fingerprint TEXT NOT NULL CHECK(length(chunker_fingerprint)=64),
 embedding_fingerprint TEXT NOT NULL CHECK(length(embedding_fingerprint)=64),
 state TEXT NOT NULL CHECK(state IN ('BUILDING','READY','RETIRED')),
 next_commit_seq INTEGER NOT NULL CHECK(next_commit_seq>0),
 generation_manifest_hash TEXT CHECK(generation_manifest_hash IS NULL OR length(generation_manifest_hash)=64)
) STRICT;
CREATE TABLE group_sources (
 group_id TEXT PRIMARY KEY NOT NULL, source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 seq_from INTEGER NOT NULL CHECK(seq_from>0),seq_to INTEGER NOT NULL CHECK(seq_to>=seq_from),
 closed_receipt_ref_json TEXT NOT NULL CHECK(json_valid(closed_receipt_ref_json)),
 UNIQUE(group_id,source_hash)
) STRICT;
CREATE TABLE indexed_groups (
 index_generation INTEGER NOT NULL REFERENCES index_generations(index_generation),group_id TEXT NOT NULL,
 source_hash TEXT NOT NULL,view_hash TEXT NOT NULL CHECK(length(view_hash)=64),
 chunk_count INTEGER NOT NULL CHECK(chunk_count>=0), -- vector_ready_count is frozen initial batch diagnostic; actual coverage is counted by upper_commit queries.
 vector_ready_count INTEGER NOT NULL CHECK(vector_ready_count>=0 AND vector_ready_count<=chunk_count),
 commit_seq INTEGER NOT NULL CHECK(commit_seq>0), materialization_receipt_json TEXT NOT NULL CHECK(json_valid(materialization_receipt_json)),
 PRIMARY KEY(index_generation,group_id), FOREIGN KEY(group_id,source_hash) REFERENCES group_sources(group_id,source_hash)
) STRICT;
CREATE TABLE session_chunks (
 rowid INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL UNIQUE,
 index_generation INTEGER NOT NULL,group_id TEXT NOT NULL,record_id TEXT NOT NULL,
 utf8_start INTEGER NOT NULL CHECK(utf8_start>=0),utf8_end INTEGER NOT NULL CHECK(utf8_end>utf8_start),
 source_hash TEXT NOT NULL CHECK(length(source_hash)=64),view_hash TEXT NOT NULL CHECK(length(view_hash)=64),
 chunker_fingerprint TEXT NOT NULL CHECK(length(chunker_fingerprint)=64),
 text_view TEXT NOT NULL CHECK(length(CAST(text_view AS BLOB))<=16384),
 provenance TEXT NOT NULL CHECK(provenance IN ('USER_INPUT','RAW_DIALOGUE','AGENT_CLAIM','TOOL_RESULT','VERIFIER_FEEDBACK')),
 validity_epoch INTEGER NOT NULL CHECK(validity_epoch>=0),commit_seq INTEGER NOT NULL CHECK(commit_seq>0),
 FOREIGN KEY(index_generation,group_id) REFERENCES indexed_groups(index_generation,group_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(index_generation,record_id,utf8_start,utf8_end,source_hash,view_hash,chunker_fingerprint),
 UNIQUE(chunk_id,index_generation,source_hash)
) STRICT;
CREATE INDEX chunks_snapshot ON session_chunks(index_generation,commit_seq,rowid);
CREATE TABLE session_vectors (
 chunk_id TEXT NOT NULL,index_generation INTEGER NOT NULL,embedding_fingerprint TEXT NOT NULL,
 dim INTEGER NOT NULL CHECK(dim>0 AND dim<=65536),vector_le_f32 BLOB NOT NULL,
 source_hash TEXT NOT NULL, commit_seq INTEGER NOT NULL CHECK(commit_seq>0),
 embedding_receipt_ref_json TEXT NOT NULL CHECK(json_valid(embedding_receipt_ref_json)),
 PRIMARY KEY(chunk_id,index_generation,embedding_fingerprint),CHECK(length(vector_le_f32)=4*dim),
 FOREIGN KEY(chunk_id,index_generation,source_hash) REFERENCES session_chunks(chunk_id,index_generation,source_hash)
) STRICT;
CREATE TABLE query_snapshots (
 snapshot_id TEXT PRIMARY KEY NOT NULL, index_generation INTEGER NOT NULL REFERENCES index_generations(index_generation),
 upper_commit INTEGER NOT NULL CHECK(upper_commit>=0), journal_highwater INTEGER NOT NULL CHECK(journal_highwater>=0),
 source_snapshot_hash TEXT NOT NULL CHECK(length(source_snapshot_hash)=64), body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
) STRICT;
CREATE TABLE search_queries (
 query_id TEXT PRIMARY KEY NOT NULL,snapshot_id TEXT NOT NULL REFERENCES query_snapshots(snapshot_id),
 owner_scope_hash TEXT NOT NULL CHECK(length(owner_scope_hash)=64),purpose TEXT NOT NULL CHECK(purpose IN ('MODEL_SEARCH','MANAGEMENT_SEARCH','CONTEXT_RECALL')),
 query_hash TEXT NOT NULL CHECK(length(query_hash)=64),options_hash TEXT NOT NULL CHECK(length(options_hash)=64),
 query_body_json TEXT NOT NULL CHECK(json_valid(query_body_json)),
 phase TEXT NOT NULL CHECK(phase IN ('SCANNING','RESULTS','EXPIRED')),
 after_rowid INTEGER NOT NULL CHECK(after_rowid>=0),heap_json TEXT NOT NULL CHECK(json_valid(heap_json)),
 final_results_json TEXT CHECK(final_results_json IS NULL OR json_valid(final_results_json)),
 query_vector_ref_json TEXT CHECK(query_vector_ref_json IS NULL OR json_valid(query_vector_ref_json)),
 elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=0),
 control_generation INTEGER NOT NULL CHECK(control_generation>0),authority_hash TEXT NOT NULL CHECK(length(authority_hash)=64),
 row_version INTEGER NOT NULL CHECK(row_version>0),CHECK(phase!='RESULTS' OR final_results_json IS NOT NULL)
) STRICT;
CREATE TABLE cursor_pages (
 token_hash TEXT PRIMARY KEY NOT NULL CHECK(length(token_hash)=64),
 query_id TEXT NOT NULL,purpose TEXT NOT NULL CHECK(purpose IN ('MODEL_SEARCH','MANAGEMENT_SEARCH','CONTEXT_RECALL','MODEL_READ','MANAGEMENT_READ')),
 snapshot_ref_json TEXT NOT NULL CHECK(json_valid(snapshot_ref_json)),owner_scope_hash TEXT NOT NULL CHECK(length(owner_scope_hash)=64),
 control_generation INTEGER NOT NULL CHECK(control_generation>0),authority_hash TEXT NOT NULL CHECK(length(authority_hash)=64),
 expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=0),
 state TEXT NOT NULL CHECK(state IN ('PENDING','MATERIALIZED')),
 position_json TEXT NOT NULL CHECK(json_valid(position_json)),
 page_json TEXT CHECK(page_json IS NULL OR json_valid(page_json)),page_hash TEXT CHECK(page_hash IS NULL OR length(page_hash)=64),
 next_token_hash TEXT,
 CHECK((state='MATERIALIZED')=(page_json IS NOT NULL AND page_hash IS NOT NULL))
) STRICT;
CREATE VIRTUAL TABLE session_words USING fts5(text_view,content='session_chunks',content_rowid='rowid',tokenize='unicode61');
CREATE VIRTUAL TABLE session_trigrams USING fts5(text_view,content='session_chunks',content_rowid='rowid',tokenize='trigram');
CREATE TRIGGER session_chunks_ai AFTER INSERT ON session_chunks BEGIN
 INSERT INTO session_words(rowid,text_view) VALUES(new.rowid,new.text_view);
 INSERT INTO session_trigrams(rowid,text_view) VALUES(new.rowid,new.text_view);
END;
CREATE TRIGGER session_chunks_ad AFTER DELETE ON session_chunks BEGIN
 INSERT INTO session_words(session_words,rowid,text_view) VALUES('delete',old.rowid,old.text_view);
 INSERT INTO session_trigrams(session_trigrams,rowid,text_view) VALUES('delete',old.rowid,old.text_view);
END;
CREATE TRIGGER group_source_immutable BEFORE UPDATE ON group_sources BEGIN SELECT RAISE(ABORT,'Journal source group immutable'); END;
CREATE TRIGGER group_source_no_replace BEFORE INSERT ON group_sources WHEN EXISTS(SELECT 1 FROM group_sources WHERE group_id=NEW.group_id)
BEGIN SELECT RAISE(ABORT,'closed group source conflict; compare Store'); END;
CREATE TRIGGER chunk_immutable BEFORE UPDATE ON session_chunks BEGIN SELECT RAISE(ABORT,'new view requires new generation'); END;
CREATE TRIGGER chunk_no_replace BEFORE INSERT ON session_chunks WHEN EXISTS(SELECT 1 FROM session_chunks WHERE chunk_id=NEW.chunk_id OR (index_generation=NEW.index_generation AND record_id=NEW.record_id AND utf8_start=NEW.utf8_start AND utf8_end=NEW.utf8_end AND source_hash=NEW.source_hash AND view_hash=NEW.view_hash AND chunker_fingerprint=NEW.chunker_fingerprint))
BEGIN SELECT RAISE(ABORT,'immutable chunk identity'); END;
CREATE TRIGGER snapshot_immutable BEFORE UPDATE ON query_snapshots BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
CREATE TRIGGER snapshot_no_replace BEFORE INSERT ON query_snapshots WHEN EXISTS(SELECT 1 FROM query_snapshots WHERE snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'snapshot identity conflict'); END;
CREATE TRIGGER page_transition BEFORE UPDATE ON cursor_pages
WHEN OLD.state!='PENDING' OR NEW.state!='MATERIALIZED' OR NEW.token_hash!=OLD.token_hash OR NEW.owner_scope_hash!=OLD.owner_scope_hash OR NEW.purpose!=OLD.purpose OR NEW.authority_hash!=OLD.authority_hash OR NEW.control_generation!=OLD.control_generation OR NEW.query_id!=OLD.query_id OR NEW.snapshot_ref_json!=OLD.snapshot_ref_json
BEGIN SELECT RAISE(ABORT,'cursor must materialize once without identity change'); END;
CREATE TRIGGER page_no_replace BEFORE INSERT ON cursor_pages WHEN EXISTS(SELECT 1 FROM cursor_pages WHERE token_hash=NEW.token_hash)
BEGIN SELECT RAISE(ABORT,'cursor replay cannot overwrite'); END;
CREATE TRIGGER generation_transition BEFORE UPDATE ON index_generations
WHEN NEW.index_generation!=OLD.index_generation OR NEW.view_policy_hash!=OLD.view_policy_hash OR NEW.chunker_fingerprint!=OLD.chunker_fingerprint OR NEW.embedding_fingerprint!=OLD.embedding_fingerprint OR NEW.next_commit_seq<OLD.next_commit_seq
 OR (NEW.state!=OLD.state AND NOT ((OLD.state='BUILDING' AND NEW.state='READY') OR (OLD.state='READY' AND NEW.state='RETIRED')))
BEGIN SELECT RAISE(ABORT,'generation identity/transition'); END;

CREATE TRIGGER indexed_group_immutable BEFORE UPDATE ON indexed_groups BEGIN SELECT RAISE(ABORT,'immutable materialized group'); END;
CREATE TRIGGER indexed_group_no_replace BEFORE INSERT ON indexed_groups WHEN EXISTS(SELECT 1 FROM indexed_groups WHERE index_generation=NEW.index_generation AND group_id=NEW.group_id)
BEGIN SELECT RAISE(ABORT,'group materialization identity conflict'); END;
CREATE TRIGGER vector_immutable BEFORE UPDATE ON session_vectors BEGIN SELECT RAISE(ABORT,'immutable vector generation'); END;
CREATE TRIGGER vector_no_replace BEFORE INSERT ON session_vectors WHEN EXISTS(SELECT 1 FROM session_vectors WHERE chunk_id=NEW.chunk_id AND index_generation=NEW.index_generation AND embedding_fingerprint=NEW.embedding_fingerprint)
BEGIN SELECT RAISE(ABORT,'vector identity conflict'); END;
CREATE TRIGGER query_initial BEFORE INSERT ON search_queries WHEN NEW.phase!='SCANNING' OR NEW.row_version!=1 OR NEW.after_rowid!=0 OR NEW.final_results_json IS NOT NULL
BEGIN SELECT RAISE(ABORT,'query must start scanning'); END;
CREATE TRIGGER query_transition BEFORE UPDATE ON search_queries WHEN
 NEW.row_version!=OLD.row_version+1 OR NEW.query_id!=OLD.query_id OR NEW.snapshot_id!=OLD.snapshot_id
 OR NEW.owner_scope_hash!=OLD.owner_scope_hash OR NEW.query_hash!=OLD.query_hash OR NEW.options_hash!=OLD.options_hash
 OR NEW.purpose!=OLD.purpose OR NEW.control_generation!=OLD.control_generation OR NEW.authority_hash!=OLD.authority_hash
 OR NEW.query_body_json!=OLD.query_body_json OR NEW.expires_at_ms!=OLD.expires_at_ms
 OR NEW.after_rowid<OLD.after_rowid OR NEW.elapsed_ms<OLD.elapsed_ms
 OR (OLD.phase='EXPIRED') OR (OLD.phase='RESULTS' AND NEW.phase!='EXPIRED')
 OR (OLD.phase='SCANNING' AND NEW.phase NOT IN ('SCANNING','RESULTS','EXPIRED'))
BEGIN SELECT RAISE(ABORT,'query identity/state CAS'); END;
CREATE TRIGGER query_no_replace BEFORE INSERT ON search_queries WHEN EXISTS(SELECT 1 FROM search_queries WHERE query_id=NEW.query_id)
BEGIN SELECT RAISE(ABORT,'query identity conflict'); END;
CREATE TRIGGER cursor_initial BEFORE INSERT ON cursor_pages WHEN NEW.state!='PENDING' OR NEW.page_json IS NOT NULL OR NEW.page_hash IS NOT NULL
BEGIN SELECT RAISE(ABORT,'cursor must start pending'); END;
CREATE TRIGGER vector_generation_binding BEFORE INSERT ON session_vectors
WHEN NOT EXISTS(SELECT 1 FROM index_generations g WHERE g.index_generation=NEW.index_generation AND g.embedding_fingerprint=NEW.embedding_fingerprint AND g.state IN ('BUILDING','READY'))
BEGIN SELECT RAISE(ABORT,'vector embedding generation mismatch'); END;
CREATE TRIGGER chunk_generation_binding BEFORE INSERT ON session_chunks
WHEN NOT EXISTS(SELECT 1 FROM index_generations g WHERE g.index_generation=NEW.index_generation AND g.chunker_fingerprint=NEW.chunker_fingerprint AND g.view_policy_hash IS NOT NULL AND g.state IN ('BUILDING','READY'))
BEGIN SELECT RAISE(ABORT,'chunk generation mismatch'); END;
