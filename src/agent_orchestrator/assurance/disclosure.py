# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Append-only disclosure contracts bound to one actual reviewer input receipt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codec import (
    AssuranceError,
    array,
    canonical,
    digest,
    fields,
    fingerprint,
    integer,
    text,
    unique_texts,
)
from .evidence import CatalogueEntry, evidence_label
from .refs import AssuranceRef


@dataclass(frozen=True, slots=True)
class DisclosureBatch:
    mission_id: str
    review_key: str
    batch_no: int
    previous_batch_hash: str | None
    entries: tuple[CatalogueEntry, ...]
    reviewer_agent_id: str
    turn_receipt_ref: AssuranceRef
    provider_input_hash: str
    visible_message_ids: tuple[str, ...]
    delivery_receipt_ref: AssuranceRef

    def __post_init__(self) -> None:
        for value in (self.mission_id, self.review_key, self.reviewer_agent_id):
            text(value)
        integer(self.batch_no)
        if self.batch_no == 0:
            if self.previous_batch_hash is not None:
                raise AssuranceError("DISCLOSURE_CHAIN_INVALID")
        else:
            digest(self.previous_batch_hash)
        if not 1 <= len(self.entries) <= 1024:
            raise AssuranceError("DISCLOSURE_ENTRIES_INVALID")
        seen = set()
        for entry in self.entries:
            if entry.label != evidence_label(self.review_key, entry.ref) or entry.label in seen:
                raise AssuranceError("DISCLOSURE_LABEL_INVALID")
            seen.add(entry.label)
        if self.turn_receipt_ref.kind != "agent_turn_receipt":
            raise AssuranceError("DISCLOSURE_TURN_REF_INVALID")
        if self.delivery_receipt_ref.kind != "disclosure_receipt":
            raise AssuranceError("DISCLOSURE_DELIVERY_REF_INVALID")
        digest(self.provider_input_hash)
        if not self.visible_message_ids:
            raise AssuranceError("DISCLOSURE_MESSAGES_REQUIRED")
        messages = unique_texts(list(self.visible_message_ids), maximum=256)
        object.__setattr__(self, "visible_message_ids", messages)
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda e: e.label)))
        canonical(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "review_key": self.review_key,
            "batch_no": self.batch_no,
            "previous_batch_hash": self.previous_batch_hash,
            "entries": [e.to_json() for e in self.entries],
            "reviewer_agent_id": self.reviewer_agent_id,
            "turn_receipt_ref": self.turn_receipt_ref.to_json(),
            "provider_input_hash": self.provider_input_hash,
            "visible_message_ids": list(self.visible_message_ids),
            "delivery_receipt_ref": self.delivery_receipt_ref.to_json(),
        }

    @property
    def content_hash(self) -> str:
        return fingerprint(self.to_json())

    @classmethod
    def from_json(cls, value: object) -> DisclosureBatch:
        row = fields(
            value,
            {
                "schema_version",
                "mission_id",
                "review_key",
                "batch_no",
                "previous_batch_hash",
                "entries",
                "reviewer_agent_id",
                "turn_receipt_ref",
                "provider_input_hash",
                "visible_message_ids",
                "delivery_receipt_ref",
            },
        )
        if integer(row["schema_version"]) != 1:
            raise AssuranceError("DISCLOSURE_SCHEMA_VERSION")
        entries = []
        for item in array(row["entries"], minimum=1, maximum=1024):
            entry = fields(item, {"label", "ref"})
            entries.append(CatalogueEntry(entry["label"], AssuranceRef.from_json(entry["ref"])))
        return cls(
            row["mission_id"],
            row["review_key"],
            row["batch_no"],
            row["previous_batch_hash"],
            tuple(entries),
            row["reviewer_agent_id"],
            AssuranceRef.from_json(row["turn_receipt_ref"]),
            row["provider_input_hash"],
            unique_texts(row["visible_message_ids"], maximum=256),
            AssuranceRef.from_json(row["delivery_receipt_ref"]),
        )


def disclosed_to_turn(
    batches: tuple[DisclosureBatch, ...],
    *,
    mission_id: str,
    review_key: str,
    agent_id: str,
    turn_receipt_ref: AssuranceRef,
    provider_input_hash: str,
) -> frozenset[str]:
    """Validate the full persisted chain, then select this exact input's exposure.

    The second invocation cannot inherit another agent/turn's private messages.
    A later same-agent request must have its own actual input manifest import.
    This is a pure chain reader; runtime receipt authentication happens upstream.
    """
    previous = None
    labels = set()
    for ordinal, batch in enumerate(batches):
        if (
            batch.mission_id != mission_id
            or batch.review_key != review_key
            or batch.batch_no != ordinal
            or batch.previous_batch_hash != previous
        ):
            raise AssuranceError("DISCLOSURE_CHAIN_INVALID")
        previous = batch.content_hash
        if (
            batch.reviewer_agent_id == agent_id
            and batch.turn_receipt_ref == turn_receipt_ref
            and batch.provider_input_hash == provider_input_hash
        ):
            labels.update(entry.label for entry in batch.entries)
    return frozenset(labels)
