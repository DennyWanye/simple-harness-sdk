"""Explicit observational schema2: durable pre-Run stage facts, no fake Run FK."""

BASE_DDL = (
    """CREATE TABLE sdk_stage_audit_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        stage_id TEXT NOT NULL,
        incarnation TEXT NOT NULL,
        operation TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL
    ) STRICT""",
    "CREATE INDEX sdk_stage_audit_idx ON sdk_stage_audit_events(stage_id,event_seq)",
    """CREATE TRIGGER sdk_stage_audit_no_update BEFORE UPDATE ON sdk_stage_audit_events
        BEGIN SELECT RAISE(ABORT,'stage audit is append only'); END""",
    """CREATE TRIGGER sdk_stage_audit_no_delete BEFORE DELETE ON sdk_stage_audit_events
        BEGIN SELECT RAISE(ABORT,'stage audit is append only'); END""",
)


def _structural(alias):
    # Raw lease tokens/owners and private snapshot bytes deliberately never copied.
    return (
        "json_object("
        + ",".join(
            f"'{key}',{alias}.{key}"
            for key in (
                "kind",
                "identity_key",
                "user_id",
                "session_id",
                "input_hash",
                "mode",
                "state",
                "private_snapshot_hash",
                "memory_result_hash",
                "memory_query_hash",
                "product_result_hash",
                "consumed_run_id",
                "consumed_continuation_id",
                "created_at",
                "updated_at",
            )
        )
        + ")"
    )


def _incarnation(alias):
    return (
        "COALESCE((SELECT incarnation FROM sdk_stage_audit_events WHERE stage_id="
        + alias
        + ".stage_id ORDER BY event_seq DESC LIMIT 1),lower(hex(randomblob(16))))"
    )


STAGE_DDL = BASE_DDL + (
    f"""CREATE TRIGGER sdk_stage_observe_insert AFTER INSERT ON context_preparation_staging
        BEGIN INSERT INTO sdk_stage_audit_events(
            stage_id,incarnation,operation,payload_json,created_at)
        VALUES(NEW.stage_id,lower(hex(randomblob(16))),'stage.created',{_structural("NEW")},NEW.updated_at);
        END""",
    f"""CREATE TRIGGER sdk_stage_observe_update AFTER UPDATE ON context_preparation_staging
        BEGIN INSERT INTO sdk_stage_audit_events(
            stage_id,incarnation,operation,payload_json,created_at)
        VALUES(NEW.stage_id,{_incarnation("NEW")},
        CASE WHEN NEW.lease_token IS NOT OLD.lease_token AND NEW.state='preparing'
             THEN 'stage.claimed'
             WHEN NEW.state='consumed' AND OLD.state<>'consumed' THEN 'stage.consumed'
             ELSE 'stage.updated' END,{_structural("NEW")},NEW.updated_at);
        END""",
    f"""CREATE TRIGGER sdk_stage_observe_delete BEFORE DELETE ON context_preparation_staging
        BEGIN INSERT INTO sdk_stage_audit_events(
            stage_id,incarnation,operation,payload_json,created_at)
        VALUES(OLD.stage_id,{_incarnation("OLD")},'stage.deleted',{_structural("OLD")},(julianday('now')-2440587.5)*86400.0);
        END""",
)

STAGE_OBJECTS = {
    "sdk_stage_audit_events",
    "sdk_stage_audit_idx",
    "sdk_stage_audit_no_update",
    "sdk_stage_audit_no_delete",
    "sdk_stage_observe_insert",
    "sdk_stage_observe_update",
    "sdk_stage_observe_delete",
}


def _release(alias):
    return (
        "json_object("
        + ",".join(
            f"'{key}',{alias}.{key}"
            for key in (
                "release_id",
                "stage_id",
                "query_hash",
                "result_hash",
                "state",
                "attempt_count",
                "retry_at",
                "created_at",
                "released_at",
            )
        )
        + ")"
    )


for _action, _alias in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
    _operation = {"INSERT": "created", "UPDATE": "updated", "DELETE": "deleted"}[_action]
    _name = "sdk_release_observe_" + _action.lower()
    STAGE_DDL += (
        f"""CREATE TRIGGER {_name} BEFORE {_action} ON memory_recall_releases
        BEGIN INSERT INTO sdk_stage_audit_events(
            stage_id,incarnation,operation,payload_json,created_at)
        VALUES({_alias}.stage_id,{_incarnation(_alias)},'release.{_operation}',
               {_release(_alias)},(julianday('now')-2440587.5)*86400.0); END""",
    )
    STAGE_OBJECTS.add(_name)


def seed_observed_legacy(connection):
    connection.execute(
        "INSERT INTO sdk_stage_audit_events("
        "stage_id,incarnation,operation,payload_json,created_at) "
        "SELECT s.stage_id,lower(hex(randomblob(16))),'stage.legacy_baseline',"
        + _structural("s")
        + ",(julianday('now')-2440587.5)*86400.0 FROM context_preparation_staging s "
        "WHERE NOT EXISTS(SELECT 1 FROM sdk_stage_audit_events a WHERE a.stage_id=s.stage_id)"
    )
    connection.execute(
        "INSERT INTO sdk_stage_audit_events("
        "stage_id,incarnation,operation,payload_json,created_at) "
        "SELECT s.stage_id,"
        + _incarnation("s")
        + ",'release.legacy_baseline',"
        + _release("s")
        + ",(julianday('now')-2440587.5)*86400.0 FROM memory_recall_releases s "
        "WHERE NOT EXISTS(SELECT 1 FROM sdk_stage_audit_events a WHERE a.stage_id=s.stage_id "
        "AND a.operation LIKE 'release.%')"
    )
