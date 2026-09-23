# SPDX-License-Identifier: Apache-2.0
"""Exact original CAS reads protected by an already committed review pin.

This is an internal preparation reader, not a disclosure or authorization API.
The use writer still checks the acting principal, root and current policy. No
URI from a request is accepted; every path comes from the original owned CAS.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..artifacts.store import ArtifactStore, ArtifactStoreError, open_nofollow
from ..assurance.codec import AssuranceError, canonical, decode, integer, text
from ..assurance.evidence import ReadItem
from ..assurance.refs import AssuranceRef
from ..contracts import Artifact
from ..contracts.operation_payloads import decode_operation_payload
from .assurance_pins import require_live_pin_locked
from .assurance_reads import (
    EpochSnapshot,
    ExactMetadata,
    read_epochs_locked,
    require_epochs_locked,
)

if TYPE_CHECKING:
    from .assurance_reads import AssuranceReader


def read_blob_metadata(reader: AssuranceReader, ref: AssuranceRef) -> ExactMetadata:
    """Blob pins use the ORIGINAL byte hash and revision, never latest metadata.

    Source id is the original source.path, pinned by its version_hash and exact
    lifecycle revision. Artifacts use their existing id/version/content_hash;
    operation payloads retain their original fixed revision 1.
    """
    with reader.store.read_view() as connection:
        reader._mission_locked(connection)
        if ref.kind == "source":
            row = connection.execute(
                "SELECT * FROM sources WHERE mission_id=? AND path=? AND version_hash=?",
                (reader.mission_id, ref.pin.id, ref.pin.content_hash),
            ).fetchone()
            if row is None:
                raise AssuranceError("SOURCE_UNAVAILABLE", ref.pin.id)
            if row["tenant_id"] != reader.tenant_id:
                raise AssuranceError("REF_SCOPE_MISMATCH", ref.pin.id)
            if row["revision"] != ref.pin.revision:
                raise AssuranceError("REF_REVISION_MISMATCH", ref.pin.id)
            return ExactMetadata(ref, canonical(dict(row)), canonical(dict(row)))
        if ref.kind != "artifact":
            raise AssuranceError("REF_KIND_UNSUPPORTED", ref.kind)
        artifact = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id=?", (ref.pin.id,)
        ).fetchone()
        payload = connection.execute(
            "SELECT * FROM operation_payload_objects WHERE object_id=?", (ref.pin.id,)
        ).fetchone()
        if artifact is not None and payload is not None:
            raise AssuranceError("REF_LOCATOR_AMBIGUOUS", ref.pin.id)
        if artifact is None and payload is None:
            raise AssuranceError("SOURCE_UNAVAILABLE", ref.pin.id)
        row = artifact if artifact is not None else payload
        if row["mission_id"] != reader.mission_id:
            raise AssuranceError("REF_SCOPE_MISMATCH", ref.pin.id)
        revision = row["version"] if artifact is not None else row["object_revision"]
        if revision != ref.pin.revision:
            raise AssuranceError("REF_REVISION_MISMATCH", ref.pin.id)
        if row["content_hash"] != ref.pin.content_hash:
            raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
        if artifact is not None:
            body = decode(row["json"])
            original = Artifact.from_json(body)
            if (original.id, original.mission_id, original.version, original.content_hash) != (
                ref.pin.id,
                reader.mission_id,
                ref.pin.revision,
                ref.pin.content_hash,
            ):
                raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
        else:
            body = dict(row)
        return ExactMetadata(ref, canonical(body), canonical(dict(row)))


def _require_pin(reader: AssuranceReader, ref: AssuranceRef, pin_id: str, review_key: str) -> None:
    require_live_pin_locked(
        reader.store.connection,
        mission_id=reader.mission_id,
        ref=ref,
        pin_id=pin_id,
        review_key=review_key,
    )


@dataclass(frozen=True, slots=True)
class PreparedBlob:
    metadata: ExactMetadata
    pin_id: str
    review_key: str
    epochs: EpochSnapshot
    data: bytes
    access_witness: ReadItem

    def require_current_locked(
        self,
        reader: AssuranceReader,
        *,
        now_ms: int,
        authorize: Callable[[AssuranceRef], ReadItem],
    ) -> None:
        """Final metadata AND current access/root/purpose barrier at original use."""
        require_epochs_locked(
            reader.store.connection, reader.mission_id, self.epochs, now_ms=now_ms
        )
        _require_pin(reader, self.metadata.ref, self.pin_id, self.review_key)
        current = read_blob_metadata(reader, self.metadata.ref)
        if current != self.metadata:
            raise AssuranceError("RECHECK_REQUIRED")
        if _authorized(authorize, self.metadata.ref) != self.access_witness:
            raise AssuranceError("RECHECK_REQUIRED")


def _authorized(
    authorize: Callable[[AssuranceRef], ReadItem],
    ref: AssuranceRef,
) -> ReadItem:
    # Fixed trusted assembly owns the current principal/root/scope/use checks.
    # A receipt or a caller-supplied ACCESS row is not accepted instead of it.
    witness = authorize(ref)
    if not isinstance(witness, ReadItem) or witness.channel != "ACCESS":
        raise AssuranceError("ACCESS_POLICY_WITNESS_REQUIRED")
    return witness


def read_pinned_blob(
    reader: AssuranceReader,
    ref: AssuranceRef,
    *,
    pin_id: str,
    review_key: str,
    cas: ArtifactStore,
    maximum_bytes: int,
    authorize: Callable[[AssuranceRef], ReadItem],
    now_ms: Callable[[], int],
) -> PreparedBlob:
    """Read full verified bytes outside the Store lock; never return a prefix.

    maximum_bytes is the actual caller's transport/preparation budget, not an
    invented claim that all artifacts fit a particular size. Failed reads leave
    the pin live for the original owner to release with its own receipt.
    """
    text(pin_id)
    text(review_key)
    integer(maximum_bytes, minimum=1)
    if reader.store.connection.in_transaction:
        raise AssuranceError("CAS_READ_INSIDE_TRANSACTION")
    if not isinstance(cas, ArtifactStore):
        raise AssuranceError("CAS_UNBOUND")
    with reader.store.read_view() as connection:
        access = _authorized(authorize, ref)
        metadata = read_blob_metadata(reader, ref)
        _require_pin(reader, ref, pin_id, review_key)
        epochs = read_epochs_locked(connection, reader.mission_id)
    body = decode(metadata.body_json)
    lifecycle = decode(metadata.lifecycle_json)
    path = cas.path_for(ref.pin.content_hash)
    if ref.kind == "artifact" and body.get("storage_uri") != str(path):
        raise AssuranceError("CAS_STORAGE_IDENTITY_MISMATCH", ref.pin.id)
    expected_size = body.get("size_bytes", body.get("byte_length"))
    if expected_size is not None and integer(expected_size) > maximum_bytes:
        raise AssuranceError("EVIDENCE_BYTE_LIMIT", ref.pin.id)
    try:
        with open_nofollow(path) as stream:
            data = stream.read(maximum_bytes + 1)
    except (ArtifactStoreError, OSError) as error:
        raise AssuranceError("SOURCE_UNAVAILABLE", ref.pin.id) from error
    if len(data) > maximum_bytes:
        raise AssuranceError("EVIDENCE_BYTE_LIMIT", ref.pin.id)
    if hashlib.sha256(data).hexdigest() != ref.pin.content_hash:
        raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
    if expected_size is not None and len(data) != expected_size:
        raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
    if "object_kind" in lifecycle:
        try:
            payload = decode_operation_payload(data, lifecycle["object_kind"])
        except (ValueError, TypeError, KeyError) as error:
            raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id) from error
        if payload.canonical_bytes() != data or payload.content_hash() != ref.pin.content_hash:
            raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
    prepared = PreparedBlob(metadata, pin_id, review_key, epochs, data, access)
    # Permission can change while CAS I/O is in progress. Do not expose bytes
    # to the preparation caller before the current guard and epochs are checked.
    with reader.store.read_view():
        prepared.require_current_locked(reader, now_ms=integer(now_ms()), authorize=authorize)
    return prepared
