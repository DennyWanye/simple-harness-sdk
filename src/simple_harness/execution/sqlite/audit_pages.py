"""Immutable safe audit projection. Never an execution database or authority."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from simple_harness.contracts import RunId, canonical_json
from simple_harness.execution.audit import (
    CommandOperationAuditPageV1,
    ContextStageOperationAuditPageV1,
    RunAuditUnavailable,
    RunAuditUsageV1,
    RunOperationAuditPageV1,
    RunOperationAuditV1,
    audit_hash,
    audit_reference,
)

from .audit import _opaque_operation, read_snapshot

FORMAT = 2
NORMALIZER = "core-terminal-stage-intervals-registered-labels-opaque-refs-v11"
MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 192 * 1024 * 1024
MAX_SECONDS = 30.0


def _namespace(database):
    stat = database.path.stat()
    return audit_hash(
        [
            str(database.path),
            stat.st_dev,
            stat.st_ino,
            getattr(stat, "st_birthtime", None),
        ]
    )


def _root(database):
    return database.path.parent / ".audit-snapshots" / _namespace(database)


def _check_run(run_id):
    if not isinstance(run_id, RunId):
        raise TypeError("run_id must use RunId")


def _hex(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _cursor(snapshot_hash, token):
    return snapshot_hash + "." + token


def _decode(cursor):
    if not isinstance(cursor, str) or len(cursor) != 129:
        raise RunAuditUnavailable("audit_cursor_invalid")
    parts = cursor.split(".")
    if len(parts) != 2 or not all(_hex(v) for v in parts):
        raise RunAuditUnavailable("audit_cursor_invalid")
    return parts


class _Spool:
    def __init__(self, connection, run_id, path):
        self.connection, self.run_id, self.path = connection, run_id, path
        self.count = self.bytes = 0
        self.started = time.monotonic()

    def check(self):
        if time.monotonic() - self.started > MAX_SECONDS:
            raise RunAuditUnavailable("audit_snapshot_timeout")
        if self.path.stat().st_size > MAX_FILE_BYTES:
            raise RunAuditUnavailable("audit_snapshot_capacity")

    def append(self, operation):
        self.check()
        operation = _opaque_operation(operation, self.run_id)
        payload = canonical_json(operation.to_json())
        self.bytes += len(payload.encode())
        if self.bytes > MAX_BYTES:
            raise RunAuditUnavailable("audit_snapshot_capacity")
        self.connection.execute(
            "INSERT INTO operations VALUES (?,?,?,?,?,?)",
            (
                operation.kind,
                operation.operation_id,
                operation.source_version,
                operation.record_type,
                operation.source_id,
                payload,
            ),
        )
        self.count += 1


def open_pages(database, run_id=None, *, page_size=256, command_id=None, stage_id=None):
    query_kind = (
        "stage" if stage_id is not None else ("command" if command_id is not None else "run")
    )
    if stage_id is not None:
        if (
            command_id is not None
            or run_id is not None
            or not isinstance(stage_id, str)
            or not stage_id
        ):
            raise ValueError("invalid stage audit identity")
        command_id = stage_id  # Shared secondary-domain transport, never a fabricated command row.
    if command_id is None:
        _check_run(run_id)
    elif run_id is not None or not isinstance(command_id, str) or not command_id:
        raise ValueError("invalid command audit identity")
    if not database.is_open:
        raise RunAuditUnavailable("audit_store_unavailable")
    if type(page_size) is not int or not 1 <= page_size <= 4096:
        raise ValueError("audit page_size must be 1..4096")
    path = None
    output = None
    try:
        namespace = _namespace(database)
        root = _root(database)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix="pending-", suffix=".sqlite", dir=root)
        os.close(fd)
        path = Path(name)
        output = sqlite3.connect(path)
        output.execute("PRAGMA synchronous=FULL")
        output.execute("PRAGMA temp_store=FILE")
        output.execute("CREATE TABLE operations(kind,operation,version,record_type,source,payload)")
        output.execute("CREATE TABLE pages(page_index INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
        output.execute("CREATE TABLE manifest(payload TEXT NOT NULL)")
        spool = _Spool(output, run_id.value if command_id is None else command_id, path)
        with database.transaction(read_only=True) as source:
            source.set_progress_handler(
                lambda: int(time.monotonic() - spool.started > MAX_SECONDS), 1000
            )
            try:
                from .command_audit import command_cut, command_incarnation, read_command_snapshot
                from .stage_audit import run_stage_cut

                if query_kind == "stage":
                    from .stage_audit import (
                        read_stage_snapshot as read_command_snapshot,
                    )
                    from .stage_audit import (
                        stage_cut as command_cut,
                    )
                    from .stage_audit import (
                        stage_incarnation as command_incarnation,
                    )

                if command_id is None:
                    header = read_snapshot(source, run_id.value, 256, operation_sink=spool)
                    incarnation = _incarnation(source, run_id.value)
                    cut = _event_cut(source, run_id.value)
                    command_source_cut = command_cut(source, run_id=run_id.value)
                    stage_source_cut = run_stage_cut(source, run_id.value)
                else:
                    header = read_command_snapshot(source, command_id, spool)
                    incarnation = command_incarnation(source, command_id)
                    cut = command_cut(source, command_id)
                    command_source_cut = cut
                    stage_source_cut = []
            finally:
                source.set_progress_handler(None, 0)
        if namespace != _namespace(database):
            raise RunAuditUnavailable("audit_dataset_changed")
        # The canonical read transaction ends before disk sorting and publication.
        output.set_progress_handler(
            lambda: int(time.monotonic() - spool.started > MAX_SECONDS), 1000
        )
        ordered = output.execute(
            "SELECT payload FROM operations ORDER BY kind,operation,version,record_type,source"
        )
        hashes, tokens = [], []
        while True:
            spool.check()
            rows = ordered.fetchmany(page_size)
            if not rows:
                break
            values = [json.loads(row[0]) for row in rows]
            hashes.append(audit_hash(values))
            tokens.append(secrets.token_hex(32))
            output.execute(
                "INSERT INTO pages VALUES (?,?)", (len(hashes) - 1, canonical_json(values))
            )
        metadata = header.to_json()
        for key in ("operations", "snapshot_hash", "truncated", "current_source_complete"):
            metadata.pop(key)
        manifest = dict(
            format=FORMAT,
            query_kind=query_kind,
            incarnation=incarnation,
            event_cut=cut,
            command_cut=command_source_cut,
            stage_cut=stage_source_cut,
            normalizer=NORMALIZER,
            source_schema=database.schema_version,
            namespace=namespace,
            header=metadata,
            page_size=page_size,
            total_operations=spool.count,
            page_hashes=hashes,
            tokens=tokens,
        )
        snapshot_hash = audit_hash(manifest)
        output.execute("INSERT INTO manifest VALUES (?)", (canonical_json(manifest),))
        output.execute("DROP TABLE operations")
        output.commit()
        spool.check()
        output.set_progress_handler(None, 0)
        output.close()
        output = None
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        target = root / (snapshot_hash + ".sqlite")
        os.replace(path, target)
        path = None
        dirfd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return read_page(
            database,
            run_id,
            command_id=None if query_kind == "stage" else command_id,
            stage_id=stage_id,
            cursor=_cursor(snapshot_hash, tokens[0]),
        )
    except RunAuditUnavailable:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        raise RunAuditUnavailable("audit_snapshot_unavailable") from None
    finally:
        if output is not None:
            output.close()
        if path is not None:
            path.unlink(missing_ok=True)
            Path(str(path) + "-journal").unlink(missing_ok=True)


def read_page(database, run_id=None, *, cursor, command_id=None, stage_id=None):
    query_kind = (
        "stage" if stage_id is not None else ("command" if command_id is not None else "run")
    )
    if stage_id is not None:
        if (
            command_id is not None
            or run_id is not None
            or not isinstance(stage_id, str)
            or not stage_id
        ):
            raise ValueError("invalid stage audit identity")
        command_id = stage_id  # Shared secondary-domain transport, never a fabricated command row.
    if command_id is None:
        _check_run(run_id)
    elif run_id is not None or not isinstance(command_id, str) or not command_id:
        raise ValueError("invalid command audit identity")
    if not database.is_open:
        raise RunAuditUnavailable("audit_store_unavailable")
    snapshot_hash, token = _decode(cursor)
    try:
        path = _root(database) / (snapshot_hash + ".sqlite")
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise RunAuditUnavailable("audit_snapshot_unavailable")
        # mode=ro cannot accidentally create missing snapshots.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT payload FROM manifest").fetchone()
            if row is None or len(row[0].encode()) > MAX_BYTES:
                raise RunAuditUnavailable("audit_manifest_invalid")
            manifest = json.loads(row[0])
            if audit_hash(manifest) != snapshot_hash:
                raise RunAuditUnavailable("audit_manifest_hash_mismatch")
            if (
                manifest["format"] != FORMAT
                or manifest["normalizer"] != NORMALIZER
                or manifest["source_schema"] != database.schema_version
                or manifest["namespace"] != _namespace(database)
            ):
                raise RunAuditUnavailable("audit_snapshot_version_unavailable")
            header = manifest["header"]
            if manifest["query_kind"] != query_kind:
                raise RunAuditUnavailable("audit_cursor_domain_mismatch")
            if command_id is not None and header[
                "stage_ref" if query_kind == "stage" else "command_ref"
            ] != audit_reference("stage" if query_kind == "stage" else "control", command_id):
                raise RunAuditUnavailable("audit_cursor_command_mismatch")
            if command_id is None and header["run_id"] != run_id.value:
                raise RunAuditUnavailable("audit_cursor_run_mismatch")
            # Prove current canonical Run ownership/start and the captured immutable
            # cut. Filesystem identity is only a location, never an incarnation.
            with database.transaction(read_only=True) as source:
                from .command_audit import command_cut, command_incarnation
                from .stage_audit import run_stage_cut

                if query_kind == "stage":
                    from .stage_audit import stage_cut as command_cut
                    from .stage_audit import stage_incarnation as command_incarnation

                cut = manifest["event_cut"]
                if command_id is None:
                    if _incarnation(source, run_id.value) != manifest["incarnation"]:
                        raise RunAuditUnavailable("audit_run_incarnation_mismatch")
                    if _event_cut(source, run_id.value, sequence=cut["sequence"]) != cut:
                        raise RunAuditUnavailable("audit_run_cut_mismatch")
                    run_stage_cut(source, run_id.value, saved=manifest["stage_cut"])
                    terminal = header.get("terminal_evidence")
                    if terminal is not None and _event_cut(
                        source, run_id.value, sequence=terminal["event_sequence"]
                    ) != {
                        "sequence": terminal["event_sequence"],
                        "source_hash": terminal["event_record_hash"],
                    }:
                        raise RunAuditUnavailable("audit_terminal_cut_mismatch")
                    command_bound = manifest["command_cut"]
                    if (
                        command_cut(source, run_id=run_id.value, sequence=command_bound["sequence"])
                        != command_bound
                    ):
                        raise RunAuditUnavailable("audit_command_cut_mismatch")
                else:
                    if command_incarnation(source, command_id) != manifest["incarnation"]:
                        raise RunAuditUnavailable("audit_command_incarnation_mismatch")
                    if command_cut(source, command_id, sequence=cut["sequence"]) != cut:
                        raise RunAuditUnavailable("audit_command_cut_mismatch")
            tokens, hashes = manifest["tokens"], manifest["page_hashes"]
            if token not in tokens:
                raise RunAuditUnavailable("audit_cursor_invalid")
            index = tokens.index(token)
            total, size = manifest["total_operations"], manifest["page_size"]
            if (
                type(size) is not int
                or not 1 <= size <= 4096
                or type(total) is not int
                or total <= 0
                or len(hashes) != (total + size - 1) // size
                or len(tokens) != len(hashes)
                or len(set(tokens)) != len(tokens)
            ):
                raise RunAuditUnavailable("audit_manifest_invalid")
            row = connection.execute(
                "SELECT payload FROM pages WHERE page_index=?", (index,)
            ).fetchone()
            if row is None or len(row[0].encode()) > MAX_BYTES:
                raise RunAuditUnavailable("audit_page_unavailable")
            values = json.loads(row[0])
            if audit_hash(values) != hashes[index]:
                raise RunAuditUnavailable("audit_page_hash_mismatch")
            if len(values) != min(size, total - index * size):
                raise RunAuditUnavailable("audit_page_count_mismatch")
            page_type = (
                RunOperationAuditPageV1
                if command_id is None
                else (
                    ContextStageOperationAuditPageV1
                    if query_kind == "stage"
                    else CommandOperationAuditPageV1
                )
            )
            owner = (
                dict(run_id=run_id.value)
                if command_id is None
                else (
                    dict(stage_ref=header["stage_ref"])
                    if query_kind == "stage"
                    else dict(command_ref=header["command_ref"])
                )
            )
            return page_type(
                **owner,
                snapshot_hash=snapshot_hash,
                page_index=index,
                page_size=size,
                total_operations=total,
                total_pages=len(hashes),
                page_hash=hashes[index],
                operations=tuple(_operation(v) for v in values),
                next_cursor=_cursor(snapshot_hash, tokens[index + 1])
                if index + 1 < len(tokens)
                else None,
                metadata=header,
            )
    except RunAuditUnavailable:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError, IndexError):
        raise RunAuditUnavailable("audit_snapshot_unavailable") from None


def _operation(value):
    fields = dict(value)
    duration = fields.pop("handoff_to_settlement_seconds")
    if fields.get("usage") is not None:
        fields["usage"] = RunAuditUsageV1(**fields["usage"])
    result = RunOperationAuditV1(**fields)
    if result.handoff_to_settlement_seconds != duration:
        raise RunAuditUnavailable("audit_page_duration_mismatch")
    return result


def _incarnation(connection, run_id):
    row = connection.execute(
        "SELECT r.run_id,r.execution_session_id,r.request_id,r.root_run_id,r.parent_run_id,"
        "r.profile_key,r.driver_kind,r.created_at,s.user_id,s.created_at AS session_created_at,"
        "a.snapshot_json,a.snapshot_hash,a.created_at AS snapshot_created_at "
        "FROM runs r JOIN execution_sessions s ON s.session_id=r.execution_session_id "
        "JOIN run_start_snapshots a ON a.run_id=r.run_id WHERE r.run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("audit_run_incarnation_unavailable")
    values = dict(row)
    if audit_hash(json.loads(values["snapshot_json"])) != values["snapshot_hash"]:
        raise RunAuditUnavailable("audit_run_incarnation_corrupt")
    return audit_hash(values)


def _event_cut(connection, run_id, *, sequence=None):
    if sequence is None:
        row = connection.execute(
            "SELECT * FROM run_events WHERE run_id=? ORDER BY durable_seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM run_events WHERE run_id=? AND durable_seq=?",
            (run_id, sequence),
        ).fetchone()
    if row is None:
        raise RunAuditUnavailable("audit_run_cut_unavailable")
    return dict(sequence=row["durable_seq"], source_hash=audit_hash(dict(row)))
