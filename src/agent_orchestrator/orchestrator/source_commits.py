# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Human/Host source commands (P3.3 D2/D7), on the existing commit/approval ledger.

The source's bytes live only in CAS. A source-change approval binds both the exact
old head revision and new byte hash; decide_approval validates and consumes it in
the same SQLite transaction as its decision. No connector or dispatch worker runs it.
"""

from __future__ import annotations

import hashlib
import posixpath
import unicodedata
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from simple_harness.contracts import canonical_json

from ..artifacts.store import ArtifactStore, ArtifactStoreError
from ..governance.permissions import Principal
from ..governance.policies import DeploymentPolicy
from .action_commits import ActionCommitError

if TYPE_CHECKING:
    from ..contracts import Event
    from ..governance.domains import DomainProfileV1
    from ..storage.store import Store

SOURCE_TRUST = "untrusted_external"
SOURCE_NOT_FOUND = "no such object for this caller"


class SourceCommitError(ActionCommitError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


class SourceCommitsMixin:
    if TYPE_CHECKING:
        _store: Store
        _source_artifact_store: ArtifactStore | None

        def domain_for(self, mission_id: str) -> DomainProfileV1: ...

        def _emit(
            self,
            event_type: str,
            mission_id: str,
            *,
            key: str,
            task_id: str | None = None,
            attempt_id: str | None = None,
            payload: Mapping[str, Any] | None = None,
            actor_type: str = ...,
            actor_id: str = ...,
        ) -> Event: ...

    def _source_scope(self, mission_id: str, tenant_id: str, path: str) -> None:
        mission = self._store.get_mission(mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise SourceCommitError("not_found", SOURCE_NOT_FOUND)
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or path != posixpath.normpath(path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise SourceCommitError("invalid_request", "source path must be canonical and relative")
        roots = self.domain_for(mission_id).source_roots
        if not any(path.startswith(root.rstrip("/") + "/") for root in roots):
            raise SourceCommitError("not_found", SOURCE_NOT_FOUND)

    def _source_cas(self) -> ArtifactStore:
        if self._source_artifact_store is None:
            raise SourceCommitError("refused", "source commands require an explicit CAS")
        return self._source_artifact_store

    def _check_source_path_available(self, mission_id: str, path: str) -> None:
        """Keep one spelling for shared components, and forbid file ancestors/aliases."""

        parts = [(part, unicodedata.normalize("NFC", part).casefold()) for part in path.split("/")]
        for source in self._store.list_sources(mission_id, active_only=True):
            existing = source["path"]
            if existing == path:
                continue  # replacing the exact head is checked separately
            conflict = False
            for (part, folded), other in zip(parts, existing.split("/"), strict=False):
                if folded != unicodedata.normalize("NFC", other).casefold():
                    break  # distinct siblings below identically spelled parents
                if part != other:
                    conflict = True  # a shared directory/file has an alias spelling
                    break
            else:
                conflict = True  # one full file path is a prefix of the other
            if conflict:
                raise SourceCommitError("conflict", "source path conflicts with an active source")

    def _source_head(self, mission_id: str, path: str, expected: str) -> dict[str, Any]:
        old = self._store.get_source(mission_id, path, expected)
        active = self._store.get_source(mission_id, path)
        if old is None:
            raise SourceCommitError("not_found", SOURCE_NOT_FOUND)
        if old["superseded_by"] is not None or (active and active["version_hash"] != expected):
            raise SourceCommitError("conflict", "source head changed")
        return old

    def register_source(
        self,
        *,
        mission_id: str,
        tenant_id: str,
        principal: Principal,
        path: str,
        content: str,
        kind: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._source_command(
            "register",
            mission_id=mission_id,
            tenant_id=tenant_id,
            principal=principal,
            path=path,
            content=content,
            kind=kind,
            idempotency_key=idempotency_key,
        )

    def supersede_source(
        self,
        *,
        mission_id: str,
        tenant_id: str,
        principal: Principal,
        path: str,
        content: str,
        kind: str,
        idempotency_key: str,
        expected_version_hash: str,
        deployment: DeploymentPolicy,
    ) -> dict[str, Any]:
        return self._source_command(
            "supersede",
            mission_id=mission_id,
            tenant_id=tenant_id,
            principal=principal,
            path=path,
            content=content,
            kind=kind,
            idempotency_key=idempotency_key,
            expected_version_hash=expected_version_hash,
            deployment=deployment,
        )

    def revoke_source(
        self,
        *,
        mission_id: str,
        tenant_id: str,
        principal: Principal,
        path: str,
        idempotency_key: str,
        expected_version_hash: str,
        reason: str,
        deployment: DeploymentPolicy,
    ) -> dict[str, Any]:
        return self._source_command(
            "revoke",
            mission_id=mission_id,
            tenant_id=tenant_id,
            principal=principal,
            path=path,
            idempotency_key=idempotency_key,
            expected_version_hash=expected_version_hash,
            reason=reason,
            deployment=deployment,
        )

    def _source_command(
        self,
        operation: str,
        *,
        mission_id: str,
        tenant_id: str,
        principal: Principal,
        path: str,
        idempotency_key: str,
        content: str | None = None,
        kind: str | None = None,
        expected_version_hash: str | None = None,
        reason: str = "",
        deployment: DeploymentPolicy | None = None,
    ) -> dict[str, Any]:
        if not isinstance(principal, Principal):
            raise SourceCommitError("refused", "source commands require an authenticated Principal")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise SourceCommitError("invalid_request", "source command needs an idempotency key")
        if operation != "revoke" and (
            not isinstance(content, str) or not isinstance(kind, str) or not kind.strip()
        ):
            raise SourceCommitError("invalid_request", "content must be text and kind non-blank")
        if operation == "revoke" and (not isinstance(reason, str) or not reason.strip()):
            raise SourceCommitError("invalid_request", "source revocation needs a reason")
        if operation != "register" and (
            not isinstance(expected_version_hash, str)
            or len(expected_version_hash) != 64
            or not set(expected_version_hash) <= set("0123456789abcdef")
        ):
            raise SourceCommitError("invalid_request", "expected_version_hash must be a SHA-256")
        try:
            data = None if content is None else content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise SourceCommitError("invalid_request", "content must be UTF-8 encodable") from error
        new_hash = None if data is None else hashlib.sha256(data).hexdigest()
        body = {
            "operation": operation,
            "mission_id": mission_id,
            "tenant_id": tenant_id,
            "path": path,
            "version_hash": new_hash,
            "kind": kind,
            "expected_version_hash": expected_version_hash,
            "reason": reason,
        }
        command_id = "source-" + _digest(
            {"tenant_id": tenant_id, "mission_id": mission_id, "idempotency_key": idempotency_key}
        )
        body_hash = _digest(body)
        with self._store.transaction():
            self._source_scope(mission_id, tenant_id, path)
            known = self._store.get_receipt(command_id)
            if known is not None:
                if known["body_hash"] != body_hash:
                    raise SourceCommitError(
                        "conflict", "source idempotency key has a different body"
                    )
                return dict(known)
            if operation != "revoke":
                self._check_source_path_available(mission_id, path)
            cas = self._source_cas()
            old = None
            if operation == "register":
                if any(s["path"] == path for s in self._store.list_sources(mission_id)):
                    raise SourceCommitError(
                        "conflict", "registered paths require a source-change approval"
                    )
            else:
                assert expected_version_hash is not None
                old = self._source_head(mission_id, path, expected_version_hash)
                if operation == "revoke" and old["revoked"]:
                    raise SourceCommitError("conflict", "source is already revoked")
                if new_hash is not None:
                    historical = self._store.get_source(mission_id, path, new_hash)
                    if historical is not None and historical["kind"] != kind:
                        raise SourceCommitError(
                            "conflict", "historical source hash has another kind"
                        )
            # Only immutable CAS bytes can precede the SQL commit. A rollback may leave
            # an unreferenced blob, never a visible source or a partly applied decision.
            if data is not None:
                try:
                    cas.put_bytes(data)
                except (ArtifactStoreError, OSError) as error:
                    raise SourceCommitError("refused", "cannot persist source CAS bytes") from error
            receipt: dict[str, Any] = {
                "command_id": command_id,
                "body_hash": body_hash,
                "mission_id": mission_id,
                "path": path,
                "version_hash": new_hash or expected_version_hash,
                "state": "REGISTERED" if operation == "register" else "PENDING",
            }
            if operation == "register":
                record = {
                    "mission_id": mission_id,
                    "tenant_id": tenant_id,
                    "path": path,
                    "version_hash": new_hash,
                    "kind": kind,
                    "trust": SOURCE_TRUST,
                    "registered_at": self._store.now,
                    "superseded_by": None,
                    "revoked": False,
                    "revision": 1,
                }
                self._store.put_source(record)
                self._emit(
                    "SourceRegistered",
                    mission_id,
                    key=command_id,
                    payload={"sources": [record]},
                    actor_type="user",
                    actor_id=principal.principal_id,
                )
            else:
                assert old is not None and deployment is not None
                request_id = "approval-" + command_id
                binding = {**body, "old_revision": old["revision"], "command_id": command_id}
                receipt.update(request_id=request_id, binding_hash=_digest(binding))
                self._store.put_approval(
                    {
                        "request_id": request_id,
                        "kind": "source_change",
                        "mission_id": mission_id,
                        "subject_key": command_id,
                        "state": "PENDING",
                        "version": 1,
                        "binding": binding,
                        "level": "L2",
                        "required_count": 1,
                        "distinct_principals": True,
                        "grant_count": 0,
                        "granted_by": [],
                        "expires_at": self._store.now + float(deployment.approval_ttl_seconds),
                        "summary": {
                            "operation": operation,
                            "path": path,
                            "expected_version_hash": expected_version_hash,
                            "version_hash": new_hash,
                            "kind": kind,
                            "reason": reason,
                        },
                        "reason": reason,
                        "comments": [],
                        "created_at": self._store.now,
                        "requested_by": principal.to_json(),
                    }
                )
                self._emit(
                    "ApprovalRequested",
                    mission_id,
                    key=request_id,
                    payload={"request_id": request_id, "kind": "source_change"},
                    actor_type="user",
                    actor_id=principal.principal_id,
                )
            self._store.insert_receipt(
                commit_id=command_id,
                kind="source_command",
                subject_id=mission_id,
                base_version=None,
                proposal_hash=body_hash,
                receipt=receipt,
            )
            return receipt

    def _validate_source_binding(self, request: Mapping[str, Any]) -> None:
        """Both decisions require an intact receipt binding, independent of source state."""

        bound = request["binding"]
        receipt = self._store.get_receipt(str(request["subject_key"]))
        mission = self._store.get_mission(str(request["mission_id"]))
        if (
            receipt is None
            or receipt.get("request_id") != request["request_id"]
            or receipt.get("command_id") != request["subject_key"]
            or receipt.get("binding_hash") != _digest(bound)
            or receipt.get("mission_id") != request["mission_id"]
            or bound.get("command_id") != request["subject_key"]
            or bound.get("mission_id") != request["mission_id"]
            or mission is None
            or bound.get("tenant_id") != mission.tenant_id
        ):
            raise SourceCommitError("conflict", "source approval binding changed")

    def _validate_source_approval(self, request: Mapping[str, Any]) -> None:
        """Grant-only current-state checks, after validating the immutable binding."""

        bound = request["binding"]
        self._source_scope(bound["mission_id"], bound["tenant_id"], bound["path"])
        if bound["operation"] == "supersede":
            self._check_source_path_available(bound["mission_id"], bound["path"])
        old = self._source_head(bound["mission_id"], bound["path"], bound["expected_version_hash"])
        if old["revision"] != bound["old_revision"]:
            raise SourceCommitError("conflict", "source head revision changed")
        try:
            cas = self._source_cas()
            cas.read(old["version_hash"])
            if bound["operation"] == "supersede":
                cas.read(bound["version_hash"])
        except ArtifactStoreError as error:
            raise SourceCommitError(
                "refused", "source CAS bytes are unavailable or changed"
            ) from error

    def _apply_source_approval(self, request: dict[str, Any], principal: Principal) -> None:
        """Consume a validated grant atomically; never called by a connector callback."""

        bound = request["binding"]
        mid, path = bound["mission_id"], bound["path"]
        old = self._source_head(mid, path, bound["expected_version_hash"])
        changed = []
        if bound["operation"] == "revoke":
            old.update(revoked=True, revision=old["revision"] + 1)
            self._store.put_source(old)
            changed.append(old)
            event_type = "SourceRevoked"
        else:
            new_hash = bound["version_hash"]
            historical = self._store.get_source(mid, path, new_hash)
            if old["version_hash"] != new_hash:
                old.update(superseded_by=new_hash, revision=old["revision"] + 1)
                self._store.put_source(old)
                changed.append(old)
            new = historical or {
                "mission_id": mid,
                "tenant_id": bound["tenant_id"],
                "path": path,
                "version_hash": new_hash,
                "kind": bound["kind"],
                "trust": SOURCE_TRUST,
                "registered_at": self._store.now,
                "revision": 0,
            }
            new.update(superseded_by=None, revoked=False, revision=new["revision"] + 1)
            self._store.put_source(new)
            changed.append(new)
            event_type = "SourceSuperseded"
        request["consumed_at"] = self._store.now
        self._store.put_approval(request)
        self._emit(
            event_type,
            mid,
            key=request["request_id"],
            payload={"request_id": request["request_id"], "sources": changed},
            actor_type="user",
            actor_id=principal.principal_id,
        )
