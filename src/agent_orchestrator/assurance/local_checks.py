# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Record actual synchronous checker runs, before trusted Commit import.

The registry is constructed by VerifierRouter assembly, never by a model/API.
This module neither manufactures execution receipts nor marks official checks.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .checks import CheckResult, Grade, grade
from .codec import AssuranceError, array, canonical, digest, fields, integer, one_of, text
from .refs import AssuranceRef


@dataclass(frozen=True, slots=True)
class AssertionOutput:
    assertion_key: str
    verdict: Grade
    output: bytes | None

    def __post_init__(self) -> None:
        text(self.assertion_key)
        if not isinstance(self.verdict, Grade) or (
            self.output is not None and not isinstance(self.output, bytes)
        ):
            raise AssuranceError("CHECK_ASSERTION_INVALID")

    def to_json(self) -> dict[str, Any]:
        return {
            "assertion_key": self.assertion_key,
            "verdict": self.verdict.value,
            "output_hash": (
                hashlib.sha256(self.output).hexdigest() if self.output is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RegisteredLocalChecker:
    spec_ref: AssuranceRef
    implementation_hash: str
    run: Callable[[], tuple[AssertionOutput, ...]]
    max_runtime_ms: int = 120_000

    def __post_init__(self) -> None:
        if self.spec_ref.kind != "check_spec":
            raise AssuranceError("CHECK_SPEC_REF_REQUIRED")
        digest(self.implementation_hash)
        if not callable(self.run):
            raise AssuranceError("CHECKER_UNAVAILABLE")
        integer(self.max_runtime_ms, minimum=1, maximum=120_000)


@dataclass(frozen=True, slots=True)
class RecordedLocalCheck:
    """Frozen JSON plus actual raw outputs for the existing artifact recorder."""

    receipt_json: str
    outputs: tuple[AssertionOutput, ...]


class LocalCheckRecorder:
    def __init__(
        self,
        checkers: Mapping[AssuranceRef, RegisteredLocalChecker],
        *,
        now_ms: Callable[[], int],
        implementation_hash: str,
    ) -> None:
        for spec, checker in checkers.items():
            if checker.spec_ref != spec:
                raise AssuranceError("CHECKER_REGISTRY_IDENTITY")
        self.checkers = MappingProxyType(dict(checkers))
        self.now_ms = now_ms
        self.implementation_hash = digest(implementation_hash)

    def record_run(
        self,
        spec: AssuranceRef,
        *,
        mission_id: str,
        subject_hash: str,
        input_manifest_hash: str,
        environment_hash: str,
    ) -> RecordedLocalCheck:
        checker = self.checkers.get(spec)
        if checker is None:
            raise AssuranceError("CHECKER_UNAVAILABLE")
        # Freeze identities before running the actual function, outside any Store lock.
        body: dict[str, Any] = {
            "schema_version": 1,
            "mission_id": text(mission_id),
            "check_spec_ref": spec.to_json(),
            "subject_hash": digest(subject_hash),
            "input_manifest_hash": digest(input_manifest_hash),
            "environment_hash": digest(environment_hash),
            "run_nonce": str(uuid.uuid4()),
            "started_at_ms": integer(self.now_ms()),
            "recorder_implementation_hash": self.implementation_hash,
        }
        outputs: tuple[AssertionOutput, ...] = ()
        started_monotonic = time.monotonic_ns()
        try:
            actual = checker.run()
            if (
                not isinstance(actual, tuple)
                or len(actual) > 256
                or any(not isinstance(row, AssertionOutput) for row in actual)
            ):
                raise AssuranceError("CHECK_ASSERTION_INVALID")
            keys = [row.assertion_key for row in actual]
            if len(keys) != len(set(keys)):
                raise AssuranceError("DUPLICATE_ASSERTION")
            outputs = actual
            # Synchronous original algorithms are not force-killed. A returned
            # over-budget run retains actual outputs but cannot count as success.
            state = (
                "SUCCEEDED"
                if time.monotonic_ns() - started_monotonic <= checker.max_runtime_ms * 1_000_000
                else "ERROR"
            )
        except Exception:
            # A failed/malformed checker is ERROR, not a semantic FAIL or a PASS.
            # Process death has no returned receipt, so consumers remain UNKNOWN.
            state = "ERROR"
        body.update(
            finished_at_ms=integer(self.now_ms()),
            execution_state=state,
            assertions=[row.to_json() for row in outputs],
        )
        return RecordedLocalCheck(canonical(body), outputs)


def validate_local_check(
    value: object,
    *,
    expected_spec: AssuranceRef,
    expected_subject_hash: str,
    expected_mission_id: str,
    assertion_key: str,
) -> tuple[str, Grade]:
    """Decode actual recorded output; this by itself establishes no provenance."""
    canonical(value)
    body = fields(
        value,
        {
            "schema_version",
            "mission_id",
            "check_spec_ref",
            "subject_hash",
            "input_manifest_hash",
            "environment_hash",
            "run_nonce",
            "started_at_ms",
            "finished_at_ms",
            "execution_state",
            "assertions",
            "recorder_implementation_hash",
        },
    )
    if integer(body["schema_version"]) != 1:
        raise AssuranceError("LOCAL_CHECK_SCHEMA_VERSION")
    spec = AssuranceRef.from_json(body["check_spec_ref"], kinds={"check_spec"})
    if (
        spec != expected_spec
        or body["subject_hash"] != expected_subject_hash
        or body["mission_id"] != expected_mission_id
    ):
        raise AssuranceError("CHECK_SOURCE_IDENTITY")
    for key in (
        "subject_hash",
        "input_manifest_hash",
        "environment_hash",
        "recorder_implementation_hash",
    ):
        digest(body[key])
    text(body["mission_id"])
    text(body["run_nonce"])
    integer(body["started_at_ms"])
    integer(body["finished_at_ms"])
    # Wall rollback is represented by the environment generation; never rewrite timestamps.
    state = one_of(body["execution_state"], {"SUCCEEDED", "ERROR", "CANCELLED"})
    values = {}
    for item in array(body["assertions"]):
        row = fields(item, {"assertion_key", "verdict", "output_hash"})
        key = text(row["assertion_key"])
        if key in values:
            raise AssuranceError("DUPLICATE_ASSERTION")
        if row["output_hash"] is not None:
            digest(row["output_hash"])
        values[key] = grade(row["verdict"])
    result = (
        values.get(text(assertion_key), Grade.UNKNOWN) if state == "SUCCEEDED" else Grade.UNKNOWN
    )
    return state, result


def normalize_local_check(
    value: object,
    *,
    expected_spec: AssuranceRef,
    expected_subject_hash: str,
    expected_mission_id: str,
    receipt_ref: AssuranceRef,
    assertion_key: str,
    source_valid: bool,
) -> CheckResult:
    """Called only AFTER exact event issuer/access/current-source verification."""
    if receipt_ref.kind != "local_check_receipt":
        raise AssuranceError("CHECK_SOURCE_IDENTITY")
    state, result = validate_local_check(
        value,
        expected_spec=expected_spec,
        expected_subject_hash=expected_subject_hash,
        expected_mission_id=expected_mission_id,
        assertion_key=assertion_key,
    )
    return CheckResult(expected_spec, receipt_ref, state, result, source_valid)
