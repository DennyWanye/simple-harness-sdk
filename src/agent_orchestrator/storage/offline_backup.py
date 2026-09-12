# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Offline, coordinated backup/restore for a macOS Host orchestration root.

This is a maintenance API, not a runtime runner. It never reconciles, dispatches,
releases a reservation, or changes an immutable request/receipt. The Host's actual
root flock, all SQLite write gates and absence of live SDK leases are required.
The directories must be exclusively owned by the Host/SDK (not arbitrary external
file writers). A successful restore is still subject to ordinary SDK recovery and
external-effect reconciliation; local rollback cannot undo an external effect.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_harness.execution.sqlite.schema import accepted_descriptor_rows

from ..artifacts.store import open_nofollow, read_verified
from ..artifacts.workspace import WorkspaceManager
from ..runtime.assembly import OrchestratorConfig, _read_context_identity, execution_db_for
from ..runtime.model_router import RuntimeProfile
from . import schema
from .store import Store

PROTOCOL = "orchestrator-offline-backup-v1"
MANIFEST = "manifest.json"
LOCK = ".instance.lock"  # Host deskpet.orchestration.lock.LOCK_NAME, same flock protocol.


class OfflineBackupError(ValueError):
    """No incomplete/unsupported backup or restore is published as successful."""


@dataclass(frozen=True)
class BackupSourceIdentity:
    """Caller attestation, not a claim that this helper built or verified a wheel."""

    commit: str
    production_inputs_sha256: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.commit) or not _hex(
            self.production_inputs_sha256
        ):
            raise OfflineBackupError("source_identity_invalid")

    def to_json(self) -> dict[str, str]:
        return {"commit": self.commit, "production_inputs_sha256": self.production_inputs_sha256}


def _hex(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with open_nofollow(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) != value:
        raise OfflineBackupError("invalid_relative_path")
    return path


def _destination(value: Path, *, outside: Path) -> Path:
    value = Path(value).absolute()
    parent = value.parent.resolve(strict=True)
    target = parent / value.name
    if (
        target == outside
        or target.is_relative_to(outside)
        # resolve() preserves spelling on case-insensitive macOS volumes. Compare
        # the existing directory identities, including Unicode filesystem aliases.
        or any(ancestor.samefile(outside) for ancestor in (parent, *parent.parents))
    ):
        raise OfflineBackupError("destination_inside_source")
    if target.exists() or target.is_symlink():
        raise OfflineBackupError("destination_exists")
    return target


def _required_source_cas(db: sqlite3.Connection) -> list[str]:
    """Source bytes have no Artifact row; retain historical and proposed versions."""
    versions = {row[0] for row in db.execute("SELECT version_hash FROM sources")}
    for row in db.execute("SELECT json FROM approvals WHERE kind='source_change'"):
        binding = json.loads(row[0])["binding"]
        # These are the explicit immutable CAS fields, never arbitrary hashes in
        # prose. Even rejected approvals retain their originally registered bytes.
        versions.add(binding["expected_version_hash"])
        if binding.get("version_hash") is not None:
            versions.add(binding["version_hash"])
    if any(not _hex(version) for version in versions):
        raise OfflineBackupError("required_source_cas_invalid_identity")
    return sorted(versions)


def _check_source_cas(root: Path, versions: list[str], files: Mapping | None = None) -> None:
    for version in versions:
        rel = "artifacts/sha256/" + version
        try:
            if _digest(root / rel) != version:
                raise OfflineBackupError("hash_differs")
            if files is not None and files.get(rel, {}).get("sha256") != version:
                raise OfflineBackupError("not_in_inventory")
        except (OSError, ValueError) as exc:
            raise OfflineBackupError("required_source_cas:" + version) from exc


@contextmanager
def _root_lock(root: Path, *, explicit: Path) -> Iterator[None]:
    if sys.platform != "darwin":
        raise OfflineBackupError("unsupported_platform: macOS Host flock/RENAME_EXCL required")
    import fcntl

    lock = Path(explicit).absolute()
    # Root is explicitly config.evidence_root, not a guessed parent of any DB.
    if lock.name != LOCK or lock.parent.resolve(strict=True) != root or lock.is_symlink():
        raise OfflineBackupError("instance_lock_path_does_not_match_config_root")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OfflineBackupError("instance_busy") from exc
        if os.stat(lock, follow_symlinks=False).st_ino != os.fstat(fd).st_ino:
            raise OfflineBackupError("instance_lock_replaced")
        yield  # A staging directory may be renamed while this same inode stays locked.
    finally:
        os.close(fd)  # Never unlink: contenders must continue locking the same inode.


@contextmanager
def _connection(path: Path, *, write_gate: bool = False) -> Iterator[sqlite3.Connection]:
    if path.is_symlink() or not path.is_file():
        raise OfflineBackupError("database_missing_or_symlink")
    mode = "rw" if write_gate else "ro"
    connection = sqlite3.connect(
        path.as_uri() + f"?mode={mode}", uri=True, isolation_level=None, timeout=0
    )
    connection.row_factory = sqlite3.Row
    try:
        if write_gate:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise OfflineBackupError("database_busy:" + path.name) from exc
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _tables(db: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema(db: sqlite3.Connection, role: str) -> list[list[Any]]:
    table = "orch_schema_migrations" if role == "orchestrator" else "sdk_schema_migrations"
    rows = tuple(
        tuple(row)
        for row in db.execute(f"SELECT version,name,checksum FROM {table} ORDER BY version")
    )
    accepted = (
        (tuple((m.version, m.name, m.checksum) for m in schema.MIGRATIONS),)
        if role == "orchestrator"
        else accepted_descriptor_rows()
    )
    if rows not in accepted:
        raise OfflineBackupError("unsupported_schema:" + role)
    if [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]:
        raise OfflineBackupError("database_integrity")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise OfflineBackupError("database_foreign_keys")
    return [list(row) for row in rows]


def _offline(db: sqlite3.Connection, role: str, now: float) -> None:
    if role == "execution":
        if db.execute(
            "SELECT 1 FROM workflow_leases WHERE expires_at>? LIMIT 1", (now,)
        ).fetchone():
            raise OfflineBackupError("live_lease: execution")
        for table in ("provider_invocations", "execution_effects"):
            if db.execute(f"SELECT 1 FROM {table} WHERE state='handed_off' LIMIT 1").fetchone():
                raise OfflineBackupError("unreconciled_handoff:" + table)
    else:
        # Business leases can outlive a normally closed executor. The Host flock
        # plus canonical SDK live leases fence actual execution, not these TTLs.
        if db.execute("SELECT 1 FROM actions WHERE state='HANDED_OFF' LIMIT 1").fetchone():
            raise OfflineBackupError("unreconciled_handoff:action")


def _absolute_references(value: str, source_root: str) -> list[dict]:
    """List exact JSON string paths; embedded prose stays byte-hashed provenance."""
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value
    result = []

    def visit(item, pointer):
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, pointer + "/" + str(index))
        elif isinstance(item, str) and source_root + "/" in item:
            result.append(
                {
                    "json_pointer": pointer,
                    "absolute_path": item
                    if item.startswith(source_root + "/") and "\n" not in item
                    else None,
                    "string_sha256": hashlib.sha256(item.encode()).hexdigest(),
                }
            )

    visit(parsed, "")
    return result


def _formal(db: sqlite3.Connection, *, source_root: str) -> tuple[dict, list]:
    """Digest every row; only Artifact.storage_uri is an explicitly derived field."""
    digests, references = {}, []
    for table in _tables(db):
        columns = [r[1] for r in db.execute(f"PRAGMA table_info({_quoted(table)})")]
        digest = hashlib.sha256()
        order = ",".join(_quoted(c) for c in columns)
        for row in db.execute(f"SELECT * FROM {_quoted(table)} ORDER BY {order}"):
            values = []
            for column, value in zip(columns, row, strict=True):
                if table == "artifacts" and column == "json":
                    item = json.loads(value)
                    item.pop("storage_uri", None)
                    value = _json(item)
                if isinstance(value, str) and source_root + "/" in value:
                    # These bytes remain immutable. Only the declared derived row
                    # mappings are rewritten. Consumers can audit provenance paths.
                    references.append(
                        {
                            "table": table,
                            "column": column,
                            "row_identity": str(row[0]),
                            "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
                            "source_prefix": source_root,
                            "references": _absolute_references(value, source_root),
                        }
                    )
                values.append({"blob_hex": value.hex()} if isinstance(value, bytes) else value)
            digest.update((_json(values) + "\n").encode())
        digests[table] = digest.hexdigest()
    sql = [
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema"
            " WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    digests["__schema_sql__"] = hashlib.sha256(_json(sql).encode()).hexdigest()
    return digests, references


def _pending(db: sqlite3.Connection, role: str) -> list[dict]:
    pending = []
    if role == "execution":
        for row in db.execute("SELECT invocation_id,state,usage_json FROM provider_invocations"):
            usage = json.loads(row[2]) if row[2] else None
            if row[1] == "unknown" or (
                row[1] in {"succeeded", "failed"}
                and (not isinstance(usage, dict) or not usage.get("usage"))
            ):
                pending.append({"kind": "provider", "id": row[0], "state": row[1]})
        pending += [
            {"kind": "tool", "id": r[0], "state": r[1]}
            for r in db.execute(
                "SELECT effect_id,state FROM execution_effects WHERE state='unknown'"
            )
        ]
    else:
        pending += [
            {"kind": "action", "id": r[0], "state": r[1]}
            for r in db.execute("SELECT action_key,state FROM actions WHERE state='UNKNOWN'")
        ]
    return sorted(pending, key=lambda item: (item["kind"], item["id"]))


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise OfflineBackupError("symlink_in_inventory")
        found.extend(Path(directory) / name for name in files)
    return sorted(found)


def _copy_file(source: Path, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open_nofollow(source) as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    mode = (stat.S_IMODE(source.stat().st_mode) & 0o700) | 0o400
    target.chmod(mode)
    digest = _digest(target)
    if _digest(source) != digest:
        raise OfflineBackupError("source_changed_during_copy")
    return {"sha256": digest, "size": target.stat().st_size, "mode": mode}


def _copy_database(source: Path, target: Path) -> None:
    # The write gate is held by a separate connection. Calling backup on a
    # connection with its own write transaction can wait on itself forever.
    with _connection(source) as reader:
        target_db = sqlite3.connect(target)
        try:
            reader.backup(target_db)
            target_db.commit()
            target_db.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_db.close()
    target.chmod(0o600)
    with target.open("rb") as stream:
        os.fsync(stream.fileno())


def _write(path: Path, value: dict) -> str:
    data = (_json(value) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    return hashlib.sha256(data).hexdigest()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(stage: Path, target: Path) -> None:
    # macOS SDK sys/stdio.h: renamex_np(..., RENAME_EXCL=0x4). No fallback
    # to os.rename: it may replace an empty directory created by a contender.
    # https://developer.apple.com/documentation/foundation/urlresourcevalues/volumesupportsexclusiverenaming
    if sys.platform != "darwin":
        raise OfflineBackupError("unsupported_atomic_directory_publish")
    for directory, _, _ in os.walk(stage, topdown=False):
        _sync_directory(Path(directory))
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(stage), os.fsencode(target), 0x4) != 0:
        raise OfflineBackupError("publish_refused:" + os.strerror(ctypes.get_errno()))
    try:
        _sync_directory(target.parent)
    except BaseException:
        # We still hold this newly published directory's instance lock.
        shutil.rmtree(target)
        raise


def _execution_inventory(root: Path, wanted: set[str]) -> None:
    actual = {p.name for p in root.glob("execution*.db")}
    contexts = {
        p.name.removesuffix(".context.json") for p in root.glob("execution*.db.context.json")
    }
    if actual != wanted or not contexts <= wanted:
        raise OfflineBackupError("execution_inventory_differs")


def _cross_bindings(connections: Mapping[str, sqlite3.Connection], dbs: list[dict]) -> None:
    """Prove that every submitted Orch identity belongs to its exact execution pool."""
    pools = {
        item["profile_id"]: connections[item["path"]] for item in dbs if item["role"] == "execution"
    }
    for row in connections["orchestrator.db"].execute(
        "SELECT agent_id,expected_turn_id,receipt_json,config_json FROM dispatch_intents"
        " WHERE agent_id IS NOT NULL"
    ):
        config = json.loads(row[3])
        pool = pools.get(config.get("runtime_profile_id") or "default")
        if pool is None:
            raise OfflineBackupError("intent_profile_missing")
        binding = pool.execute(
            "SELECT config_json FROM base_agent_bindings_v1 WHERE agent_id=?", (row[0],)
        ).fetchone()
        if binding is None or json.loads(binding[0]) != config.get("agent_config"):
            raise OfflineBackupError("intent_agent_binding_differs")
        if row[2] is not None:
            turn = pool.execute(
                "SELECT agent_id FROM base_agent_turns_v1 WHERE turn_id=?", (row[1],)
            ).fetchone()
            receipt = json.loads(row[2])
            if turn is None or turn[0] != row[0] or receipt.get("turn_id") != row[1]:
                raise OfflineBackupError("intent_turn_receipt_differs")


def backup_offline(
    config: OrchestratorConfig,
    profiles: Mapping[str, RuntimeProfile],
    workspaces: WorkspaceManager,
    *,
    instance_lock_path: Path,
    destination: Path,
    source_identity: BackupSourceIdentity,
) -> dict:
    """Publish a new bundle; fail closed unless the explicit Host root is offline."""
    root = config.evidence_root.resolve(strict=True)
    target = _destination(destination, outside=root)
    if (
        workspaces.root.is_symlink()
        or workspaces.artifact_store.root.is_symlink()
        or workspaces.root.resolve() != config.workspaces_root.resolve()
        or workspaces.artifact_store.root.resolve() != root / "artifacts"
    ):
        raise OfflineBackupError("workspace_mapping_differs_from_config")
    if not profiles or any(
        key != p.profile_id or not re.fullmatch(r"[\w.-]+", key) for key, p in profiles.items()
    ):
        raise OfflineBackupError("invalid_profiles")
    dbs: list[dict[str, Any]] = [
        {"path": config.orchestrator_db.name, "role": "orchestrator", "profile_id": None}
    ]
    for key, profile in sorted(profiles.items()):
        dbs.append(
            {
                "path": execution_db_for(config, key).name,
                "role": "execution",
                "profile_id": key,
                "profile": profile.to_json(),
                "price_snapshot": None
                if profile.price_table is None
                else profile.price_table.estimator().snapshot_json(),
            }
        )
    expected = {d["path"] for d in dbs if d["role"] == "execution"}
    stage = None
    try:
        with _root_lock(root, explicit=instance_lock_path), ExitStack() as locks:
            _execution_inventory(root, expected)
            connections = {
                d["path"]: locks.enter_context(_connection(root / d["path"], write_gate=True))
                for d in sorted(dbs, key=lambda item: item["path"])
            }
            if len(
                {((root / name).stat().st_dev, (root / name).stat().st_ino) for name in connections}
            ) != len(connections):
                raise OfflineBackupError("duplicate_database_identity")
            now = time.time()
            for item in dbs:
                db = connections[item["path"]]
                item["schema"] = _schema(db, item["role"])
                _offline(db, item["role"], now)
                if item["role"] == "execution":
                    frozen = _read_context_identity(root / item["path"])
                    if frozen != profiles[item["profile_id"]].context_snapshot():
                        raise OfflineBackupError("context_identity_differs")
                    for row in db.execute("SELECT config_json FROM base_agent_bindings_v1"):
                        if json.loads(row[0]).get("model_profile_ref") != item["profile_id"]:
                            raise OfflineBackupError("execution_profile_binding_differs")
            _cross_bindings(connections, dbs)
            required_sources = _required_source_cas(connections[config.orchestrator_db.name])
            _check_source_cas(root, required_sources)
            stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.offline-", dir=target.parent))
            locks.enter_context(_root_lock(stage, explicit=stage / LOCK))
            manifest: dict[str, Any] = {
                "protocol": PROTOCOL,
                "source_root": str(root),
                "source_identity": source_identity.to_json(),
                "config": config.to_json(),
                "databases": dbs,
                "files": {},
                "formal_fingerprints": {},
                "immutable_source_references": {},
                "derived_paths": [],
                "required_source_cas": required_sources,
                "pending_reconciliation": {},
                "directories": ["workspaces", "artifacts", "artifacts/sha256"],
            }
            for item in dbs:
                name = item["path"]
                _copy_database(root / name, stage / name)
                with _connection(stage / name) as copied:
                    manifest["formal_fingerprints"][name], refs = _formal(
                        copied, source_root=str(root)
                    )
                    manifest["immutable_source_references"][name] = refs
                    manifest["pending_reconciliation"][name] = _pending(copied, item["role"])
                manifest["files"][name] = {
                    "sha256": _digest(stage / name),
                    "size": (stage / name).stat().st_size,
                    "mode": 0o600,
                }
                sidecar = root / (name + ".context.json")
                if sidecar.exists():
                    manifest["files"][sidecar.name] = _copy_file(sidecar, stage / sidecar.name)
            for folder in (workspaces.root.resolve(), workspaces.artifact_store.root.resolve()):
                if folder.is_symlink():
                    raise OfflineBackupError("symlink_in_inventory")
                if not folder.exists():
                    continue
                for directory, _, _ in os.walk(folder):
                    manifest["directories"].append(Path(directory).relative_to(root).as_posix())
                for path in _files(folder):
                    rel = path.relative_to(root).as_posix()
                    if folder.name == "artifacts" and (
                        path.parent.name != "sha256"
                        or not _hex(path.name)
                        or _digest(path) != path.name
                    ):
                        raise OfflineBackupError("invalid_cas_inventory")
                    manifest["files"][rel] = _copy_file(path, stage / rel)
            store = Store.open_readonly(root / config.orchestrator_db.name)
            try:
                for artifact in store.list_all_artifacts():
                    if not artifact.storage_uri:
                        raise OfflineBackupError("artifact_unavailable:" + artifact.id)
                    read_verified(artifact)
                    uri = Path(artifact.storage_uri)
                    if not uri.is_relative_to(root):
                        raise OfflineBackupError("artifact_outside_owned_root")
                    rel = (
                        workspaces.artifact_store.path_for(artifact.content_hash)
                        .relative_to(root)
                        .as_posix()
                    )
                    if rel not in manifest["files"]:
                        manifest["files"][rel] = _copy_file(uri, stage / rel)
                    manifest["derived_paths"].append(
                        {
                            "artifact_id": artifact.id,
                            "old_storage_uri": artifact.storage_uri,
                            "relative_storage_uri": rel,
                            "content_hash": artifact.content_hash,
                        }
                    )
            finally:
                store.close()
            _check_source_cas(stage, required_sources, manifest["files"])
            manifest["directories"] = sorted(set(manifest["directories"]))
            for directory in manifest["directories"]:
                (stage / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
            _execution_inventory(root, expected)
            # Check source files again while holding all write/instance gates.
            for rel, meta in manifest["files"].items():
                if (
                    rel not in connections
                    and (root / rel).exists()
                    and _digest(root / rel) != meta["sha256"]
                ):
                    raise OfflineBackupError("source_changed_during_copy")
            digest = _write(stage / MANIFEST, manifest)
            _publish(stage, target)
            return {
                "path": str(target),
                "manifest_sha256": digest,
                "pending_reconciliation": manifest["pending_reconciliation"],
            }
    except (OfflineBackupError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise OfflineBackupError("backup_failed:" + str(exc)) from exc
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def restore_offline(bundle: Path, *, destination: Path, expected_manifest_sha256: str) -> dict:
    """Restore to a new isolated root; do not run any Agent or external lookup."""
    root = Path(bundle).resolve(strict=True)
    target = _destination(destination, outside=root)
    stage = None
    try:
        with _root_lock(root, explicit=root / LOCK):
            if (
                not _hex(expected_manifest_sha256)
                or _digest(root / MANIFEST) != expected_manifest_sha256
            ):
                raise OfflineBackupError("manifest_hash_differs")
            manifest = json.loads((root / MANIFEST).read_text())
            if manifest["protocol"] != PROTOCOL:
                raise OfflineBackupError("manifest_protocol")
            files = manifest["files"]
            actual = {p.relative_to(root).as_posix() for p in _files(root)} - {LOCK, MANIFEST}
            if actual != set(files):
                raise OfflineBackupError("file_inventory_differs")
            names = [item["path"] for item in manifest["databases"]]
            profiles = [
                item["profile_id"] for item in manifest["databases"] if item["role"] == "execution"
            ]
            if len(set(names)) != len(names) or len(set(profiles)) != len(profiles):
                raise OfflineBackupError("duplicate_database_identity")
            if not set(names) <= set(files) or [
                d["path"] for d in manifest["databases"] if d["role"] == "orchestrator"
            ] != ["orchestrator.db"]:
                raise OfflineBackupError("database_manifest_invalid")
            for item in manifest["databases"]:
                _relative(item["path"])
                if item["role"] == "orchestrator":
                    continue
                profile_id = item["profile_id"]
                expected_name = (
                    "execution.db" if profile_id == "default" else f"execution-{profile_id}.db"
                )
                if (
                    item["role"] != "execution"
                    or item["path"] != expected_name
                    or item["profile"]["profile_id"] != profile_id
                ):
                    raise OfflineBackupError("execution_profile_identity_differs")
            for rel, metadata in files.items():
                path = root / _relative(rel)
                if path.stat().st_size != metadata["size"] or _digest(path) != metadata["sha256"]:
                    raise OfflineBackupError("file_hash_differs:" + rel)
            stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.offline-", dir=target.parent))
            with _root_lock(stage, explicit=stage / LOCK):
                for rel in manifest["directories"]:
                    (stage / _relative(rel)).mkdir(parents=True, exist_ok=True, mode=0o700)
                for rel in files:
                    _copy_file(root / rel, stage / rel)
                for item in manifest["databases"]:
                    with _connection(stage / item["path"]) as db:
                        if _schema(db, item["role"]) != item["schema"]:
                            raise OfflineBackupError("schema_differs")
                        fingerprints, _ = _formal(db, source_root=manifest["source_root"])
                        if fingerprints != manifest["formal_fingerprints"][item["path"]]:
                            raise OfflineBackupError("formal_identity_differs")
                        if item["role"] == "execution":
                            if _read_context_identity(stage / item["path"]) != item["profile"].get(
                                "runtime_context"
                            ):
                                raise OfflineBackupError("context_identity_differs")
                with ExitStack() as readers:
                    connections = {
                        item["path"]: readers.enter_context(_connection(stage / item["path"]))
                        for item in manifest["databases"]
                    }
                    _cross_bindings(connections, manifest["databases"])
                    required_sources = _required_source_cas(connections["orchestrator.db"])
                    if required_sources != manifest.get("required_source_cas", []):
                        raise OfflineBackupError("required_source_cas_inventory_differs")
                    _check_source_cas(stage, required_sources, files)
                store = Store.open(stage / "orchestrator.db")
                try:
                    changes = []
                    for change in manifest["derived_paths"]:
                        artifact = store.get_artifact(change["artifact_id"])
                        if (
                            artifact is None
                            or artifact.storage_uri != change["old_storage_uri"]
                            or artifact.content_hash != change["content_hash"]
                        ):
                            raise OfflineBackupError("derived_path_identity_differs")
                        rel = _relative(change["relative_storage_uri"])
                        if _digest(stage / rel) != artifact.content_hash:
                            raise OfflineBackupError("artifact_hash_differs")
                        changes.append((artifact.id, str(target / rel)))
                    store.update_artifact_storage(changes)
                    fingerprints, _ = _formal(store.connection, source_root=manifest["source_root"])
                    if fingerprints != manifest["formal_fingerprints"]["orchestrator.db"]:
                        raise OfflineBackupError("restore_changed_formal_rows")
                    store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    store.close()
                receipt = {
                    "protocol": PROTOCOL,
                    "backup_manifest_sha256": expected_manifest_sha256,
                    "source_identity": manifest["source_identity"],
                    "formal_fingerprints": manifest["formal_fingerprints"],
                    "derived_paths": manifest["derived_paths"],
                    "immutable_source_references": manifest["immutable_source_references"],
                    "pending_reconciliation": manifest["pending_reconciliation"],
                    "external_effects_rolled_back": False,
                    "runtime_started": False,
                }
                _write(stage / "restore-manifest.json", receipt)
                _publish(stage, target)
                return receipt
    except (OfflineBackupError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise OfflineBackupError("restore_failed:" + str(exc)) from exc
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


__all__ = ("BackupSourceIdentity", "OfflineBackupError", "backup_offline", "restore_offline")
