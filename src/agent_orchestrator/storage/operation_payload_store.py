# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Immutable metadata bridge for D2 CAS operation payloads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..artifacts.store import ArtifactStore, ArtifactStoreError
from ..contracts.models import ContractError
from ..contracts.operation_payloads import PayloadKind, _Payload, decode_operation_payload
from ..contracts.semantic_base import TypedRef, TypedRefKind, identifier
from .store import Store, StoreConflict

OBJECT_REVISION = 1
_MEDIA_TYPE = "application/vnd.simple-harness.operation-payload+json"


@dataclass(frozen=True, slots=True)
class StoredOperationPayload:
    object_id: str
    mission_id: str
    kind: PayloadKind
    content_hash: str
    media_type: str
    storage_uri: str
    source_receipt_id: str
    payload: _Payload


class OperationPayloadStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    def put_payload(
        self,
        *,
        mission_id: str,
        object_id: str,
        kind: PayloadKind | str,
        payload: _Payload,
        source_receipt_id: str,
        cas: ArtifactStore,
        media_type: str = _MEDIA_TYPE,
    ) -> TypedRef:
        """CAS bytes may be written before T0; only metadata is transactionally registered."""
        expected_kind = PayloadKind(kind)
        if not isinstance(payload, _Payload) or payload.KIND is not expected_kind:
            raise StoreConflict("operation payload kind does not match its typed document")
        mission = identifier(mission_id, "mission_id")
        object_key = identifier(object_id, "object_id")
        receipt_id = identifier(source_receipt_id, "source_receipt_id")
        if not isinstance(media_type, str) or not media_type:
            raise StoreConflict("operation payload media_type is required")
        if getattr(self._store, "_reading", False) or getattr(self._store, "_depth", 0) < 1:
            raise StoreConflict("operation payload writer requires the caller's Store transaction")
        raw = payload.canonical_bytes()
        digest = payload.content_hash()
        try:
            stored_hash = cas.put_bytes(raw)
        except (ArtifactStoreError, OSError) as error:
            raise StoreConflict("operation payload CAS write unavailable") from error
        if stored_hash != digest:
            raise StoreConflict("operation payload CAS digest mismatch")
        uri = str(cas.path_for(digest))
        fields = (
            mission,
            str(expected_kind),
            OBJECT_REVISION,
            digest,
            len(raw),
            media_type,
            uri,
            receipt_id,
        )
        with self._store.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM missions WHERE mission_id=?", (mission,)
            ).fetchone()
            if exists is None:
                raise StoreConflict("operation payload mission is unavailable")
            receipt = self._store.get_receipt(receipt_id)
            if receipt is None or str(receipt.get("mission_id", "")) != mission:
                raise StoreConflict(
                    "operation payload source receipt is unavailable to this mission"
                )
            existing = connection.execute(
                "SELECT mission_id,object_kind,object_revision,content_hash,byte_length,media_type,storage_uri,source_receipt_id "
                "FROM operation_payload_objects WHERE object_id=?",
                (object_key,),
            ).fetchone()
            if existing is not None:
                old = tuple(existing)
                if old != fields:
                    raise StoreConflict("operation payload immutable metadata conflict")
                return TypedRef(TypedRefKind.ARTIFACT, object_key, OBJECT_REVISION, digest)
            try:
                connection.execute(
                    "INSERT INTO operation_payload_objects("
                    "object_id,mission_id,object_kind,object_revision,content_hash,byte_length,media_type,storage_uri,source_receipt_id,created_at_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (object_key, *fields, int(self._store.now * 1000)),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict("operation payload metadata could not be stored") from error
        return TypedRef(TypedRefKind.ARTIFACT, object_key, OBJECT_REVISION, digest)

    def get_payload(
        self,
        *,
        mission_id: str,
        ref: TypedRef,
        expected_kind: PayloadKind | str,
        cas: ArtifactStore,
    ) -> StoredOperationPayload:
        mission = identifier(mission_id, "mission_id")
        kind = PayloadKind(expected_kind)
        if (
            not isinstance(ref, TypedRef)
            or ref.kind is not TypedRefKind.ARTIFACT
            or ref.revision != OBJECT_REVISION
        ):
            raise StoreConflict("operation payload reference kind/revision mismatch")
        row = self._store.connection.execute(
            "SELECT mission_id,object_kind,content_hash,byte_length,media_type,storage_uri,source_receipt_id "
            "FROM operation_payload_objects WHERE object_id=?",
            (ref.id,),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != mission
            or str(row[1]) != str(kind)
            or str(row[2]) != ref.content_hash
        ):
            raise StoreConflict("operation payload source unavailable")
        receipt = self._store.get_receipt(str(row[6]))
        if receipt is None or str(receipt.get("mission_id", "")) != mission:
            raise StoreConflict("operation payload source receipt is unavailable to this mission")
        if str(row[5]) != str(cas.path_for(ref.content_hash)):
            raise StoreConflict("operation payload storage identity mismatch")
        try:
            raw = cas.read(ref.content_hash)
        except (ArtifactStoreError, OSError) as error:
            raise StoreConflict("operation payload bytes unavailable") from error
        if len(raw) != int(row[3]):
            raise StoreConflict("operation payload byte length mismatch")
        try:
            payload = decode_operation_payload(raw, kind)
        except ContractError as error:
            raise StoreConflict("operation payload codec/hash mismatch") from error
        if payload.content_hash() != ref.content_hash or payload.canonical_bytes() != raw:
            raise StoreConflict("operation payload canonical bytes mismatch")
        return StoredOperationPayload(
            ref.id, mission, kind, ref.content_hash, str(row[4]), str(row[5]), str(row[6]), payload
        )


__all__ = ("OBJECT_REVISION", "OperationPayloadStore", "StoredOperationPayload")
