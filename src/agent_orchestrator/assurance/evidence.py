# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Deterministic labels and complete read sets; no claim of actual disclosure."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .codec import AssuranceError, canonical, digest, fields, fingerprint, one_of, text
from .refs import AssuranceRef


def evidence_label(review_key: str, ref: AssuranceRef) -> str:
    return "ev-" + fingerprint(
        {
            "kind": "assurance-evidence-label-v1",
            "review_key": text(review_key),
            "ref": ref.to_json(),
        }
    )


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    label: str
    ref: AssuranceRef

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "ref": self.ref.to_json()}


def build_catalogue(review_key: str, refs: Iterable[AssuranceRef]) -> tuple[CatalogueEntry, ...]:
    entries: dict[str, AssuranceRef] = {}
    for ref in refs:
        label = evidence_label(review_key, ref)
        previous = entries.setdefault(label, ref)
        if previous != ref:
            raise AssuranceError("LABEL_COLLISION")
    result = tuple(CatalogueEntry(label, entries[label]) for label in sorted(entries))
    canonical([entry.to_json() for entry in result])
    return result


def resolve_evidence_ids(
    review_key: str,
    labels: tuple[str, ...],
    catalogue: tuple[CatalogueEntry, ...],
    disclosed: frozenset[str],
) -> tuple[AssuranceRef, ...]:
    """Caller must obtain `disclosed` from the exact output turn's input manifests."""
    if len(labels) != len(set(labels)):
        raise AssuranceError("DUPLICATE_EVIDENCE_LABEL")
    known: dict[str, AssuranceRef] = {}
    for entry in catalogue:
        if entry.label != evidence_label(review_key, entry.ref):
            raise AssuranceError("CATALOGUE_LABEL_BINDING")
        if entry.label in known and known[entry.label] != entry.ref:
            raise AssuranceError("LABEL_COLLISION")
        known[entry.label] = entry.ref
    if any(label not in known or label not in disclosed for label in labels):
        raise AssuranceError("UNEXPOSED_EVIDENCE")
    return tuple(known[label] for label in labels)


@dataclass(frozen=True, slots=True)
class ReadItem:
    channel: str
    key: str
    fingerprint: str
    coverage: str = "COMPLETE"

    def __post_init__(self) -> None:
        one_of(self.channel, {"OBJECT", "QUERY_SET", "ACCESS", "POLICY"})
        text(self.key)
        digest(self.fingerprint)
        if self.coverage != "COMPLETE":
            raise AssuranceError("READSET_INCOMPLETE")

    def to_json(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "key": self.key,
            "fingerprint": self.fingerprint,
            "coverage": self.coverage,
        }

    @classmethod
    def from_json(cls, value: object) -> ReadItem:
        return cls(**fields(value, {"channel", "key", "fingerprint", "coverage"}))


def canonical_read_set(rows: Iterable[ReadItem]) -> tuple[ReadItem, ...]:
    seen: set[tuple[str, str]] = set()
    result = []
    for row in rows:
        key = row.channel, row.key
        if key in seen:
            raise AssuranceError("DUPLICATE_READ_KEY")
        seen.add(key)
        result.append(row)
        if len(result) > 20_000:
            raise AssuranceError("READSET_LIMIT")
    result.sort(key=lambda row: (row.channel, row.key))
    # The independent byte limit can be reached long before the item limit.
    canonical([row.to_json() for row in result])
    return tuple(result)
