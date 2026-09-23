# SPDX-License-Identifier: Apache-2.0
"""Authenticated root management through the original Commit receipt writer."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from ..assurance.codec import AssuranceError, canonical, fingerprint, integer, text
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.root_gate import STATE_FILE, AssuranceRootGate, CurrentReadPermission
from ..governance.permissions import Principal
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic
from .assurance_clock import observe_assurance_clock

if TYPE_CHECKING:
    from .commit_service import CommitService

ReadAuthority = Callable[[Principal, str, str, AssuranceRef, str], CurrentReadPermission]


@contextmanager
def _root_writer(gate: AssuranceRootGate):
    if gate.store.connection.in_transaction:
        raise AssuranceError("ROOT_MANAGEMENT_REQUIRES_OUTER_COMMIT")
    lock = gate.root / ".assurance-root-state.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _publish(gate: AssuranceRootGate, ref: AssuranceRef, root_id: str) -> None:
    # Called AFTER the original DB transaction commits. A crash before replace
    # leaves a missing/old file, which cannot match the new current receipt head.
    with gate.store.read_view():
        head = gate._head_locked(root_id)
        if head is None or head["commit_id"] != ref.pin.id:
            raise AssuranceError("ROOT_GRANT_SUPERSEDED")
    data = canonical(
        {"schema_version": 1, "root_incarnation_id": root_id, "receipt_ref": ref.to_json()}
    ).encode()
    temporary = gate.root / ("." + STATE_FILE + "." + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, gate.root / STATE_FILE)
        directory = os.open(gate.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _caller(principal: Principal, tenant_id: str, command_id: str) -> None:
    if not isinstance(principal, Principal):
        raise AssuranceError("CURRENT_AUTHENTICATED_PRINCIPAL_REQUIRED")
    text(tenant_id)
    text(command_id)


def _initialize_clock(commit: CommitService, root_id: str, now_ms: int) -> None:
    """Use the existing installation writer, preserving a restored high-water mark."""
    with atomic(commit.store) as connection:
        if connection.execute(
            "SELECT 1 FROM assurance_environment_state WHERE singleton=1"
        ).fetchone():
            return
        body = {"root_incarnation_id": root_id, "installed_at_ms": now_ms}
        receipt_id = "assurance-environment-install:" + fingerprint(body)
        commit.store.insert_receipt(
            commit_id=receipt_id,
            kind="AssuranceEnvironmentInstalled",
            subject_id=root_id,
            base_version=0,
            proposal_hash=fingerprint(body),
            receipt=body,
        )
        AssuranceStore(commit.store).initialize_environment(
            receipt=AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body))),
            root_incarnation_id=root_id,
            now_ms=now_ms,
        )


def _native_origin(gate: AssuranceRootGate) -> str:
    known_empty_files = {
        "orchestrator.db",
        "orchestrator.db-wal",
        "orchestrator.db-shm",
        ".assurance-root-state.lock",
        ".instance.lock",
    }
    has_history = (
        gate.store.connection.execute(
            "SELECT 1 FROM missions UNION ALL SELECT 1 FROM commit_receipts LIMIT 1"
        ).fetchone()
        is not None
    )
    has_other_content = any(path.name not in known_empty_files for path in gate.root.iterdir())
    return "HISTORICAL" if has_history or has_other_content else "EMPTY"


def install_native_root(
    commit: CommitService,
    gate: AssuranceRootGate,
    *,
    principal: Principal,
    tenant_id: str,
    command_id: str,
) -> AssuranceRef:
    """Explicit authenticated installation for a new OR historical native root.

    A nonempty unknown root is never inferred to be a new empty one. The receipt
    records which case was actually observed. Managed restores use reauthorize.
    """
    _caller(principal, tenant_id, command_id)
    if commit.store is not gate.store:
        raise AssuranceError("ROOT_STORE_MISMATCH")
    request = {
        "root_path": str(gate.root),
        "tenant_id": tenant_id,
        "principal_id": principal.principal_id,
        "command_id": command_id,
    }
    receipt_id = "assurance-root-install:" + fingerprint(request)
    with _root_writer(gate):
        if any(
            (gate.root / name).exists() or (gate.root / name).is_symlink()
            for name in ("restore-manifest.json", "restore-quarantine.json")
        ):
            raise AssuranceError("RESTORED_ROOT_REQUIRES_REAUTHORIZATION")
        with atomic(commit.store) as connection:
            old = commit.store.get_receipt(receipt_id)
            if old is None:
                existing = connection.execute(
                    "SELECT 1 FROM commit_receipts WHERE kind='AssuranceRootInstalled' "
                    "AND json_extract(receipt_json,'$.root_path')=? LIMIT 1",
                    (str(gate.root),),
                ).fetchone()
                if existing is not None or (gate.root / STATE_FILE).exists():
                    raise AssuranceError("ROOT_INSTALL_COMMAND_REQUIRED")
                body = {
                    **request,
                    "schema_version": 1,
                    "mode": "NATIVE_INSTALLATION",
                    "root_incarnation_id": "root-" + uuid4().hex,
                    "restore_manifest_hash": None,
                    "installation_origin": _native_origin(gate),
                    "issued_at_ms": integer(int(commit.store.now * 1000)),
                }
                commit.store.insert_receipt(
                    commit_id=receipt_id,
                    kind="AssuranceRootInstalled",
                    subject_id=body["root_incarnation_id"],
                    base_version=0,
                    proposal_hash=fingerprint(body),
                    receipt=body,
                )
            else:
                body = dict(old)
                if any(body.get(key) != value for key, value in request.items()):
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
            ref = AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body)))
            _initialize_clock(commit, body["root_incarnation_id"], int(commit.store.now * 1000))
        _publish(gate, ref, body["root_incarnation_id"])
        gate.require_execution()
        return ref


def reauthorize_restored_read(
    commit: CommitService,
    gate: AssuranceRootGate,
    *,
    principal: Principal,
    tenant_id: str,
    command_id: str,
    root_incarnation_id: str,
    restore_manifest_hash: str,
    targets: Sequence[tuple[str, AssuranceRef, str]],
    authority: ReadAuthority,
    ttl_ms: int = 86_400_000,
) -> AssuranceRef:
    _caller(principal, tenant_id, command_id)
    if commit.store is not gate.store:
        raise AssuranceError("ROOT_STORE_MISMATCH")
    integer(ttl_ms, minimum=1, maximum=86_400_000)
    if not callable(authority):
        raise AssuranceError("CURRENT_READ_AUTHORITY_UNAVAILABLE")
    if not 1 <= len(targets) <= 256:
        raise AssuranceError("ROOT_READ_TARGET_LIMIT")
    requested = []
    for mission_id, ref, purpose in targets:
        text(mission_id)
        if not isinstance(ref, AssuranceRef) or purpose not in {"DISCLOSE", "CONTEXT"}:
            raise AssuranceError("ROOT_READ_TARGET_INVALID")
        requested.append({"mission_id": mission_id, "ref": ref.to_json(), "purpose": purpose})
    requested.sort(key=canonical)
    if len({canonical(row) for row in requested}) != len(requested):
        raise AssuranceError("ROOT_READ_TARGET_DUPLICATE")
    request = {
        "root_path": str(gate.root),
        "root_incarnation_id": text(root_incarnation_id),
        "restore_manifest_hash": restore_manifest_hash,
        "tenant_id": tenant_id,
        "principal_id": principal.principal_id,
        "command_id": command_id,
        "requested_targets": requested,
        "ttl_ms": ttl_ms,
    }
    request_hash = fingerprint(request)
    receipt_id = "assurance-root-read:" + fingerprint(
        {
            "root": root_incarnation_id,
            "principal": principal.principal_id,
            "tenant": tenant_id,
            "command": command_id,
        }
    )
    with _root_writer(gate):
        identity = gate.restored_identity()
        gate.check_restored_integrity()
        if (
            identity.root_incarnation_id != root_incarnation_id
            or identity.restore_manifest_hash != restore_manifest_hash
        ):
            raise AssuranceError("ROOT_MANIFEST_MISMATCH")
        # Current authority calls are outside the Store transaction. The adapter
        # must consult external owners where applicable; there is no cache fallback.
        verified = []
        metadata = []
        now = integer(int(commit.store.now * 1000))
        _initialize_clock(commit, root_incarnation_id, now)
        if observe_assurance_clock(commit, now_ms=now).state != "STABLE":
            raise AssuranceError("TIME_DISCONTINUITY")
        expiry = now + ttl_ms
        for target in requested:
            ref = AssuranceRef.from_json(target["ref"])
            permission = authority(
                principal, tenant_id, target["mission_id"], ref, target["purpose"]
            )
            if not isinstance(permission, CurrentReadPermission) or permission.not_after_ms <= now:
                raise AssuranceError("CURRENT_READ_AUTHORITY_UNAVAILABLE")
            reader = AssuranceReader(
                commit.store, tenant_id=tenant_id, mission_id=target["mission_id"]
            )
            metadata.append((reader, reader.read_exact_metadata(ref)))
            verified.append(
                {
                    **target,
                    "access": permission.access.to_json(),
                    "policy": permission.policy.to_json(),
                }
            )
            expiry = min(expiry, permission.not_after_ms)
        now = integer(int(commit.store.now * 1000))
        if observe_assurance_clock(commit, now_ms=now).state != "STABLE":
            raise AssuranceError("TIME_DISCONTINUITY")
        if now >= expiry:
            raise AssuranceError("READ_AUTHORITY_EXPIRED")
        with atomic(commit.store) as connection:
            for reader, captured in metadata:
                if reader.read_exact_metadata(captured.ref) != captured:
                    raise AssuranceError("RECHECK_REQUIRED")
            old = commit.store.get_receipt(receipt_id)
            if old is not None:
                body = dict(old)
                if body.get("request_hash") != request_hash or body.get("targets") != verified:
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
                if not body["issued_at_ms"] <= now < body["not_after_ms"]:
                    raise AssuranceError("ROOT_READ_GRANT_EXPIRED")
            else:
                body = {
                    **request,
                    "schema_version": 1,
                    "mode": "READ_ONLY_REAUTHORIZED",
                    "request_hash": request_hash,
                    "targets": verified,
                    "issued_at_ms": now,
                    "not_after_ms": expiry,
                }
                commit.store.insert_receipt(
                    commit_id=receipt_id,
                    kind="AssuranceRestoredReadAuthorized",
                    subject_id=root_incarnation_id,
                    base_version=0,
                    proposal_hash=fingerprint(body),
                    receipt=body,
                )
                connection.execute(
                    "UPDATE assurance_environment_state "
                    "SET epoch=epoch+1,row_version=row_version+1,"
                    "change_receipt_id=? WHERE singleton=1",
                    (receipt_id,),
                )
            ref = AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body)))
        _publish(gate, ref, root_incarnation_id)
        return ref
