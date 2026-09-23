# SPDX-License-Identifier: Apache-2.0
"""Frozen Assurance review side contracts; original ReviewPackage bytes stay intact."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .checks import CriterionPolicy, Formula
from .codec import (
    AssuranceError,
    array,
    canonical,
    decode,
    digest,
    fields,
    fingerprint,
    integer,
    one_of,
    text,
    unique_texts,
)
from .evidence import ReadItem, canonical_read_set, evidence_label
from .refs import AssuranceRef, Pin

REVIEW_PURPOSES = frozenset(
    {
        "TASK_CONTENT",
        "METHOD_PLAN",
        "COMPOSITION",
        "ACTION_PROPOSAL",
        "OPERATION_OUTCOME",
        "MISSION_FINAL",
    }
)

REVIEW_CODEC_VERSION = "assurance-review-reply-v2"


@dataclass(frozen=True, slots=True)
class ReviewRecordBinding:
    """Authenticated interpretation sidecar; decoding alone confers no authority."""

    body_json: str

    def __post_init__(self) -> None:
        row = fields(
            decode(self.body_json),
            {
                "schema_version",
                "mission_id",
                "review_key",
                "package_ref",
                "record_ref",
                "reviewer_agent_id",
                "reviewer_turn_ref",
                "raw_output_ref",
                "raw_output_hash",
                "codec_version",
                "consumed_check_refs",
                "exposed_evidence_refs",
                "evidence_manifest_hash",
                "binding_read_set",
                "disclosure_refs",
                "invocation_ordinal",
            },
        )
        if integer(row["schema_version"]) != 2:
            raise AssuranceError("REVIEW_RECORD_BINDING_VERSION")
        for key in ("mission_id", "review_key", "reviewer_agent_id", "codec_version"):
            text(row[key])
        for key in ("package_ref", "record_ref"):
            Pin.from_json(row[key])
        AssuranceRef.from_json(row["reviewer_turn_ref"], kinds={"agent_turn_receipt"})
        raw = AssuranceRef.from_json(row["raw_output_ref"])
        if raw.pin.content_hash != digest(row["raw_output_hash"]):
            raise AssuranceError("REVIEW_RAW_HASH_MISMATCH")
        digest(row["evidence_manifest_hash"])
        integer(row["invocation_ordinal"], minimum=1, maximum=2)
        for key, kind, limit in (
            ("consumed_check_refs", "check_binding", 256),
            ("disclosure_refs", "disclosure_receipt", 128),
        ):
            refs = tuple(
                AssuranceRef.from_json(item, kinds={kind})
                for item in array(row[key], maximum=limit)
            )
            if len(refs) != len(set(refs)):
                raise AssuranceError("DUPLICATE_REVIEW_REF")
            row[key] = [ref.to_json() for ref in sorted(refs, key=lambda ref: ref.key)]
        row["exposed_evidence_refs"] = catalogue(row["exposed_evidence_refs"], row["review_key"])
        row["binding_read_set"] = [
            item.to_json()
            for item in canonical_read_set(
                ReadItem.from_json(item)
                for item in array(row["binding_read_set"], minimum=1, maximum=20_000)
            )
        ]
        object.__setattr__(self, "body_json", canonical(row))

    @classmethod
    def from_json(cls, value: object) -> ReviewRecordBinding:
        return cls(canonical(value))

    def to_json(self) -> dict[str, Any]:
        return decode(self.body_json)

    @property
    def content_hash(self) -> str:
        return fingerprint(self.to_json())


def catalogue(value: object, review_key: str) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in array(value, maximum=1024):
        row = fields(item, {"label", "ref"})
        ref = AssuranceRef.from_json(row["ref"])
        if row["label"] != evidence_label(review_key, ref) or row["label"] in seen:
            raise AssuranceError("CATALOGUE_LABEL_BINDING")
        seen.add(row["label"])
        result.append({"label": row["label"], "ref": ref.to_json()})
    return sorted(result, key=lambda row: row["label"])


@dataclass(frozen=True, slots=True)
class AssuranceReviewBinding:
    """Bounded canonical immutable document; each property read returns fresh JSON."""

    body_json: str

    def __post_init__(self) -> None:
        row = fields(
            decode(self.body_json),
            {
                "schema_version",
                "mission_id",
                "review_key",
                "package_ref",
                "round_no",
                "subject",
                "requirements_ref",
                "criterion_ids",
                "mandatory_ids",
                "formula",
                "check_requirements",
                "evidence_catalogue",
                "producer_agent_ids",
                "reviewer_policy_ref",
                "read_set",
                "context_policy_ref",
                "criterion_policy_ref",
                "catalogue_hash",
            },
        )
        if integer(row["schema_version"]) != 2:
            raise AssuranceError("REVIEW_BINDING_VERSION")
        text(row["mission_id"])
        review_key = text(row["review_key"])
        integer(row["round_no"], minimum=1)
        for key in ("package_ref", "requirements_ref", "reviewer_policy_ref", "context_policy_ref"):
            Pin.from_json(row[key])
        subject = fields(
            row["subject"],
            {
                "purpose",
                "target",
                "owner_task_ref",
                "occurrence_id",
                "completion_scope_ref",
                "method_instance_ref",
                "input_manifest_hash",
                "output_manifest_hash",
            },
        )
        one_of(subject["purpose"], REVIEW_PURPOSES)
        AssuranceRef.from_json(subject["target"])
        Pin.from_json(subject["owner_task_ref"])
        for key in ("completion_scope_ref", "method_instance_ref"):
            if subject[key] is not None:
                Pin.from_json(subject[key])
        if subject["occurrence_id"] is not None:
            text(subject["occurrence_id"])
        digest(subject["input_manifest_hash"])
        if subject["output_manifest_hash"] is not None:
            digest(subject["output_manifest_hash"])
        criteria = unique_texts(row["criterion_ids"], maximum=256)
        mandatory = unique_texts(row["mandatory_ids"], maximum=256)
        authors = unique_texts(row["producer_agent_ids"], maximum=256)
        if not criteria or not authors or not set(mandatory) <= set(criteria):
            raise AssuranceError("REVIEW_CATALOGUE_INVALID")
        Formula.from_json(row["formula"], frozenset(criteria))
        policies = tuple(
            CriterionPolicy.from_json(item)
            for item in array(row["check_requirements"], minimum=1, maximum=256)
        )
        if len(policies) != len(criteria) or {p.criterion_id for p in policies} != set(criteria):
            raise AssuranceError("POLICY_CATALOGUE_MISMATCH")
        AssuranceRef.from_json(row["criterion_policy_ref"], kinds={"check_policy"})
        entries = catalogue(row["evidence_catalogue"], review_key)
        if fingerprint(entries) != digest(row["catalogue_hash"]):
            raise AssuranceError("CATALOGUE_HASH_MISMATCH")
        reads = canonical_read_set(
            ReadItem.from_json(item) for item in array(row["read_set"], minimum=1, maximum=20_000)
        )
        row.update(
            criterion_ids=list(criteria),
            mandatory_ids=list(mandatory),
            producer_agent_ids=list(authors),
            evidence_catalogue=entries,
            check_requirements=[
                p.to_json() for p in sorted(policies, key=lambda p: p.criterion_id)
            ],
            read_set=[item.to_json() for item in reads],
        )
        object.__setattr__(self, "body_json", canonical(row))

    @classmethod
    def from_json(cls, value: object) -> AssuranceReviewBinding:
        return cls(canonical(value))

    def to_json(self) -> dict[str, Any]:
        return decode(self.body_json)

    @property
    def content_hash(self) -> str:
        return fingerprint(self.to_json())


@dataclass(frozen=True, slots=True)
class ReviewInvocation:
    body_json: str

    def __post_init__(self) -> None:
        row = fields(
            decode(self.body_json),
            {
                "schema_version",
                "mission_id",
                "review_key",
                "ordinal",
                "reason",
                "prior_failure_receipt_ref",
                "dispatch_intent_id",
                "subject_id",
                "creation_key",
                "input_id",
                "input_hash",
                "catalogue_hash",
                "reservation_fact_ref",
                "source_receipt_ref",
            },
        )
        if integer(row["schema_version"]) != 1:
            raise AssuranceError("REVIEW_INVOCATION_VERSION")
        ordinal = integer(row["ordinal"], minimum=1, maximum=2)
        for key in (
            "mission_id",
            "review_key",
            "dispatch_intent_id",
            "subject_id",
            "creation_key",
            "input_id",
        ):
            text(row[key])
        for key in ("input_hash", "catalogue_hash"):
            digest(row[key])
        expected = row["mission_id"] + ":assurance:" + row["review_key"] + ":" + str(ordinal)
        if (
            row["subject_id"] != expected
            or row["creation_key"] != expected
            or row["input_id"] != "assurance-input:" + row["review_key"] + ":" + str(ordinal)
            or row["reason"] != ("INITIAL" if ordinal == 1 else "FORMAT_REPAIR")
        ):
            raise AssuranceError("REVIEW_INVOCATION_IDENTITY")
        prior = row["prior_failure_receipt_ref"]
        if ordinal == 1 and prior is not None:
            raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
        if ordinal == 2:
            AssuranceRef.from_json(prior, kinds={"commit_receipt"})
        AssuranceRef.from_json(row["reservation_fact_ref"], kinds={"reservation_fact"})
        AssuranceRef.from_json(row["source_receipt_ref"], kinds={"commit_receipt"})
        object.__setattr__(self, "body_json", canonical(row))

    @classmethod
    def from_json(cls, value: object) -> ReviewInvocation:
        return cls(canonical(value))

    def to_json(self) -> dict[str, Any]:
        return decode(self.body_json)

    @property
    def content_hash(self) -> str:
        return fingerprint(self.to_json())
