# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Pure three-valued check/review decisions, independent of authority admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .codec import (
    AssuranceError,
    array,
    canonical,
    fields,
    integer,
    one_of,
    text,
    unique_texts,
)
from .refs import AssuranceRef


class Grade(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


def grade(value: object) -> Grade:
    return Grade(one_of(value, {"PASS", "FAIL", "UNKNOWN"}))


def tri_all(grades: Sequence[Grade]) -> Grade:
    if not grades:
        raise AssuranceError("EMPTY_CHECK_SET")
    if Grade.FAIL in grades:
        return Grade.FAIL
    return Grade.PASS if all(g is Grade.PASS for g in grades) else Grade.UNKNOWN


def tri_any(grades: Sequence[Grade]) -> Grade:
    if not grades:
        raise AssuranceError("EMPTY_CHECK_SET")
    if Grade.PASS in grades:
        return Grade.PASS
    return Grade.FAIL if all(g is Grade.FAIL for g in grades) else Grade.UNKNOWN


@dataclass(frozen=True, slots=True)
class Formula:
    operator: str
    criterion: str | None = None
    children: tuple[Formula, ...] = ()

    def __post_init__(self) -> None:
        if self.operator == "criterion":
            text(self.criterion)
            if self.children:
                raise AssuranceError("FORMULA_INVALID")
        elif self.operator in {"all", "any"}:
            if self.criterion is not None or not 1 <= len(self.children) <= 256:
                raise AssuranceError("FORMULA_INVALID")
        else:
            raise AssuranceError("FORMULA_INVALID")

    @classmethod
    def from_json(cls, value: object, criteria: frozenset[str]) -> Formula:
        canonical(value)
        count = 0

        def visit(node: object, depth: int) -> Formula:
            nonlocal count
            count += 1
            if depth > 16 or count > 512:
                raise AssuranceError("FORMULA_LIMIT")
            if not isinstance(node, dict) or len(node) != 1:
                raise AssuranceError("FORMULA_INVALID")
            operator = next(iter(node))
            if operator == "criterion":
                name = text(node[operator])
                if name not in criteria:
                    raise AssuranceError("FORMULA_UNKNOWN_CRITERION", name)
                return cls(operator, criterion=name)
            if operator not in {"all", "any"}:
                raise AssuranceError("FORMULA_INVALID")
            return cls(
                operator,
                children=tuple(
                    visit(child, depth + 1) for child in array(node[operator], minimum=1)
                ),
            )

        return visit(value, 0)

    def evaluate(self, grades: Mapping[str, Grade]) -> tuple[Grade, frozenset[str]]:
        if self.operator == "criterion":
            if self.criterion not in grades:
                raise AssuranceError("FORMULA_UNKNOWN_CRITERION")
            value = grades[self.criterion]
            return value, frozenset({self.criterion}) if value is Grade.PASS else frozenset()
        evaluated = [child.evaluate(grades) for child in self.children]
        if self.operator == "any":
            for value, witness in evaluated:
                if value is Grade.PASS:
                    return value, witness  # Frozen formula order, never model ranking.
            return tri_any([value for value, _ in evaluated]), frozenset()
        result = tri_all([value for value, _ in evaluated])
        return result, (
            frozenset().union(*(w for _, w in evaluated)) if result is Grade.PASS else frozenset()
        )


@dataclass(frozen=True, slots=True)
class CriterionPolicy:
    criterion_id: str
    mode: str
    any_check_sets: tuple[tuple[AssuranceRef, ...], ...]

    def __post_init__(self) -> None:
        text(self.criterion_id)
        if self.mode == "SEMANTIC":
            if self.any_check_sets:
                raise AssuranceError("SEMANTIC_HAS_CHECKS")
        elif self.mode == "CHECKED":
            if not 1 <= len(self.any_check_sets) <= 16:
                raise AssuranceError("CHECKED_GROUPS_REQUIRED")
            seen = set()
            for group in self.any_check_sets:
                if not 1 <= len(group) <= 32 or len(set(group)) != len(group):
                    raise AssuranceError("CHECKED_GROUP_INVALID")
                if any(ref.kind != "check_spec" for ref in group):
                    raise AssuranceError("CHECK_SPEC_REF_REQUIRED")
                identity = frozenset(group)
                if identity in seen:
                    raise AssuranceError("DUPLICATE_CHECK_GROUP")
                seen.add(identity)
        else:
            raise AssuranceError("CHECK_POLICY_UNRESOLVED")
        normalized = tuple(
            sorted(
                (tuple(sorted(group, key=lambda ref: ref.key)) for group in self.any_check_sets),
                key=lambda group: tuple(ref.key for ref in group),
            )
        )
        object.__setattr__(self, "any_check_sets", normalized)

    @classmethod
    def from_json(cls, value: object) -> CriterionPolicy:
        row = fields(value, {"criterion_id", "mode", "any_check_sets"})
        groups = array(row["any_check_sets"], maximum=16)
        return cls(
            row["criterion_id"],
            row["mode"],
            tuple(
                tuple(
                    AssuranceRef.from_json(ref, kinds={"check_spec"})
                    for ref in array(group, minimum=1, maximum=32)
                )
                for group in groups
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "mode": self.mode,
            "any_check_sets": [[ref.to_json() for ref in group] for group in self.any_check_sets],
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Normalized result AFTER the importer verifies producer, subject and current use.

    This value is not an authorization token; none of these pure functions writes
    an official review, acceptance or receipt.
    """

    spec: AssuranceRef
    receipt: AssuranceRef
    execution_state: str
    assertion_grade: Grade
    source_valid: bool

    def __post_init__(self) -> None:
        if self.spec.kind != "check_spec" or self.receipt.kind not in {
            "execution_receipt",
            "local_check_receipt",
        }:
            raise AssuranceError("CHECK_REF_INVALID")
        one_of(self.execution_state, {"SUCCEEDED", "ERROR", "CANCELLED", "NOT_RUN", "RUNNING"})
        if not isinstance(self.assertion_grade, Grade) or type(self.source_valid) is not bool:
            raise AssuranceError("CHECK_RESULT_TYPE")

    @property
    def effective(self) -> Grade:
        return (
            self.assertion_grade
            if self.source_valid and self.execution_state == "SUCCEEDED"
            else Grade.UNKNOWN
        )


@dataclass(frozen=True, slots=True)
class CheckGate:
    grade: Grade | None  # None = NOT_APPLICABLE; never fabricate a PASS receipt.
    consumed: tuple[AssuranceRef, ...]
    reason: str


def evaluate_check_gate(
    policy: CriterionPolicy, results: Mapping[AssuranceRef, CheckResult]
) -> CheckGate:
    if policy.mode == "SEMANTIC":
        return CheckGate(None, (), "CHECK_NOT_APPLICABLE")
    evaluated = []
    for group in policy.any_check_sets:
        rows = [results.get(spec) for spec in group]
        if any(row is not None and row.spec != spec for row, spec in zip(rows, group)):
            raise AssuranceError("CHECK_RESULT_IDENTITY")
        result = tri_all([row.effective if row else Grade.UNKNOWN for row in rows])
        consumed = tuple(sorted({row.receipt for row in rows if row}, key=lambda ref: ref.key))
        evaluated.append((result, consumed))
    for result, consumed in evaluated:
        if result is Grade.PASS:
            return CheckGate(result, consumed, "CHECK_GROUP_SATISFIED")
    result = tri_any([value for value, _ in evaluated])
    return CheckGate(
        result,
        tuple(sorted({ref for _, refs in evaluated for ref in refs}, key=lambda ref: ref.key)),
        "CHECK_GROUPS_FAILED" if result is Grade.FAIL else "CHECK_EVIDENCE_INCOMPLETE",
    )


@dataclass(frozen=True, slots=True)
class Assessment:
    criterion_id: str
    verdict: Grade
    evidence_ids: tuple[str, ...]
    reason: str
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    criterion_id: str
    severity: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewReply:
    verdict: str
    assessments: tuple[Assessment, ...]
    findings: tuple[Finding, ...]

    @classmethod
    def from_json(cls, value: object) -> ReviewReply:
        canonical(value)
        row = fields(value, {"schema_version", "verdict", "assessments", "findings"})
        if integer(row["schema_version"]) != 2:
            raise AssuranceError("REVIEW_SCHEMA_VERSION")
        verdict = one_of(row["verdict"], {"ACCEPT", "REWORK", "INCONCLUSIVE", "REJECTED"})
        assessments = []
        ids = set()
        for item in array(row["assessments"], minimum=1):
            a = fields(item, {"criterion_id", "verdict", "evidence_ids", "reason", "limitations"})
            name = text(a["criterion_id"])
            if name in ids:
                raise AssuranceError("DUPLICATE_CRITERION")
            ids.add(name)
            assessments.append(
                Assessment(
                    name,
                    grade(a["verdict"]),
                    unique_texts(a["evidence_ids"], maximum=64),
                    text(a["reason"], limit=2000),
                    unique_texts(a["limitations"], maximum=16),
                )
            )
        findings = []
        for item in array(row["findings"], maximum=128):
            f = fields(item, {"criterion_id", "severity", "reason"})
            name = text(f["criterion_id"])
            if name not in ids:
                raise AssuranceError("FINDING_SCOPE")
            findings.append(
                Finding(
                    name,
                    one_of(f["severity"], {"BLOCKER", "WARNING", "INFO"}),
                    text(f["reason"], limit=2000),
                )
            )
        return cls(verdict, tuple(assessments), tuple(findings))


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    acceptable: bool
    effective_grades: Mapping[str, Grade]
    success_witness: frozenset[str]
    consumed_receipts: tuple[AssuranceRef, ...]
    reasons: tuple[str, ...]


def decide_review(
    reply: ReviewReply,
    formula: Formula,
    mandatory: tuple[str, ...],
    policies: Mapping[str, CriterionPolicy],
    results: Mapping[AssuranceRef, CheckResult],
) -> ReviewDecision:
    if len(mandatory) != len(set(mandatory)) or not set(mandatory) <= policies.keys():
        raise AssuranceError("MANDATORY_CRITERIA_INVALID")
    gates = {}
    grades = {}
    reasons = []
    for assessment in reply.assessments:
        name = assessment.criterion_id
        policy = policies.get(name)
        if policy is None or policy.criterion_id != name:
            raise AssuranceError("CHECK_POLICY_UNRESOLVED")
        if name in grades:
            raise AssuranceError("DUPLICATE_CRITERION")
        gate = evaluate_check_gate(policy, results)
        gates[name] = gate
        grades[name] = (
            assessment.verdict if gate.grade is None else tri_all((assessment.verdict, gate.grade))
        )
        reasons.append(f"{name}:{gate.reason}")
    if grades.keys() != policies.keys():
        raise AssuranceError("POLICY_CATALOGUE_MISMATCH")
    for finding in reply.findings:
        if finding.criterion_id not in grades:
            raise AssuranceError("FINDING_SCOPE")
        if finding.severity == "BLOCKER":
            grades[finding.criterion_id] = Grade.FAIL
    result, witness = formula.evaluate(grades)
    if mandatory:
        result = tri_all((result, *(grades[name] for name in mandatory)))
        if result is Grade.PASS:
            witness = witness.union(mandatory)
    acceptable = reply.verdict == "ACCEPT" and result is Grade.PASS
    selected = witness if acceptable else grades.keys()
    consumed = tuple(
        sorted({ref for name in selected for ref in gates[name].consumed}, key=lambda ref: ref.key)
    )
    return ReviewDecision(
        acceptable,
        MappingProxyType(grades),
        witness if acceptable else frozenset(),
        consumed,
        tuple(reasons),
    )
