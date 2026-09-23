# SPDX-License-Identifier: Apache-2.0
"""Immutable projection of one real check assertion; not a current-use grant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .checks import Grade, grade
from .codec import AssuranceError, array, canonical, digest, fields, integer, one_of, text
from .refs import AssuranceRef, Pin


@dataclass(frozen=True, slots=True)
class CheckBinding:
    mission_id: str
    check_spec_ref: AssuranceRef
    subject_hash: str
    input_manifest_hash: str
    output_manifest_hash: str | None
    assertion_key: str
    execution_ref: AssuranceRef
    adapter_ref: Pin
    environment_hash: str
    scope_hash: str
    execution_state: str
    verdict: Grade
    evidence_refs: tuple[AssuranceRef, ...]
    observed_at_ms: int
    not_after_ms: int | None

    def __post_init__(self) -> None:
        text(self.mission_id)
        text(self.assertion_key)
        for value in (
            self.subject_hash,
            self.input_manifest_hash,
            self.environment_hash,
            self.scope_hash,
        ):
            digest(value)
        if self.output_manifest_hash is not None:
            digest(self.output_manifest_hash)
        if (
            not isinstance(self.check_spec_ref, AssuranceRef)
            or self.check_spec_ref.kind != "check_spec"
            or not isinstance(self.execution_ref, AssuranceRef)
            or self.execution_ref.kind not in {"local_check_receipt", "execution_receipt"}
            or not isinstance(self.adapter_ref, Pin)
        ):
            raise AssuranceError("CHECK_BINDING_REF_INVALID")
        one_of(self.execution_state, {"SUCCEEDED", "ERROR", "CANCELLED"})
        if not isinstance(self.verdict, Grade):
            raise AssuranceError("CHECK_BINDING_VERDICT_INVALID")
        if self.execution_state != "SUCCEEDED" and self.verdict != Grade.UNKNOWN:
            raise AssuranceError("CHECK_EXECUTION_NOT_SUCCEEDED")
        if (
            len(self.evidence_refs) > 256
            or any(not isinstance(ref, AssuranceRef) for ref in self.evidence_refs)
            or len(set(self.evidence_refs)) != len(self.evidence_refs)
        ):
            raise AssuranceError("CHECK_EVIDENCE_INVALID")
        object.__setattr__(
            self, "evidence_refs", tuple(sorted(self.evidence_refs, key=lambda r: r.key))
        )
        integer(self.observed_at_ms)
        if self.not_after_ms is not None:
            integer(self.not_after_ms)
        canonical(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "mission_id": self.mission_id,
            "check_spec_ref": self.check_spec_ref.to_json(),
            "subject_hash": self.subject_hash,
            "input_manifest_hash": self.input_manifest_hash,
            "output_manifest_hash": self.output_manifest_hash,
            "assertion_key": self.assertion_key,
            "execution_ref": self.execution_ref.to_json(),
            "adapter_ref": self.adapter_ref.to_json(),
            "environment_hash": self.environment_hash,
            "scope_hash": self.scope_hash,
            "execution_state": self.execution_state,
            "verdict": self.verdict.value,
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "observed_at_ms": self.observed_at_ms,
            "not_after_ms": self.not_after_ms,
        }

    @classmethod
    def from_json(cls, value: object) -> CheckBinding:
        row = fields(
            value,
            {
                "schema_version",
                "mission_id",
                "check_spec_ref",
                "subject_hash",
                "input_manifest_hash",
                "output_manifest_hash",
                "assertion_key",
                "execution_ref",
                "adapter_ref",
                "environment_hash",
                "scope_hash",
                "execution_state",
                "verdict",
                "evidence_refs",
                "observed_at_ms",
                "not_after_ms",
            },
        )
        if integer(row["schema_version"]) != 2:
            raise AssuranceError("CHECK_BINDING_VERSION")
        args = {key: value for key, value in row.items() if key != "schema_version"}
        for key in ("check_spec_ref", "execution_ref"):
            args[key] = AssuranceRef.from_json(args[key])
        args["adapter_ref"] = Pin.from_json(args["adapter_ref"])
        args["verdict"] = grade(args["verdict"])
        args["evidence_refs"] = tuple(
            AssuranceRef.from_json(r) for r in array(args["evidence_refs"])
        )
        return cls(**args)
