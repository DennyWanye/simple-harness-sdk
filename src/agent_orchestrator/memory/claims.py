# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Claim grading (§14.3 / §25.3, plan D4-2): the *system* decides how far a claim of an
accepted result rises, from the verification that actually ran — never from the
Agent's confidence.

* VERIFIED  — the claim cites a ``pytest:<target>`` that the ``code_test`` layer of this
  very verification ran and passed (the exact target, or a run directory that contains
  the cited file).  "一个测试通过只支持其覆盖范围内的结论" (ORCH §12.4).
* SUPPORTED — the result passed and the claim cites at least one *trusted* piece of
  evidence (a registered artifact, a pytest target, a tool run).
* unsupported — every cited item comes from an untrusted external source (D4-12) or
  cannot be resolved: the claim stays UNDER_REVIEW with ``grade=unsupported``.

Evidence reference grammar (data, parsed leniently): ``pytest:<path>``, ``file:<path>``,
``artifact:<path>``, ``tool-run:<id>``, ``knowledge:<id>``, or a bare workspace path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts import ClaimStatus

TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTED = "untrusted_external"
TRUST_UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    raw: str
    kind: str  # pytest | path | tool-run | knowledge | text
    target: str
    trust: str

    def to_json(self) -> dict[str, Any]:
        return {"ref": self.raw, "kind": self.kind, "target": self.target, "trust": self.trust}


def _normalise(path: str) -> str:
    return path.strip().strip("/").replace("\\", "/")


def parse_evidence(
    raw: str,
    *,
    artifact_paths: Sequence[str],
    untrusted_prefixes: Sequence[str],
    ran_targets: Mapping[str, bool],
) -> EvidenceRef:
    text = raw.strip()
    kind, target = "text", text
    for prefix in ("pytest:", "file:", "artifact:", "tool-run:", "knowledge:"):
        if text.startswith(prefix):
            kind = "pytest" if prefix == "pytest:" else prefix.rstrip(":")
            target = _normalise(text[len(prefix) :])
            if kind in {"file", "artifact"}:
                kind = "path"
            break
    else:
        # a bare path is an artifact citation; only an explicit ``pytest:`` item claims
        # coverage by a test run (the Worker must say which check backs the claim)
        candidate = _normalise(text)
        if candidate in set(artifact_paths):
            kind, target = "path", candidate
    if kind in {"pytest", "path"} and _is_untrusted(target, untrusted_prefixes):
        return EvidenceRef(raw, kind, target, TRUST_UNTRUSTED)
    if kind == "text" and _is_untrusted(_normalise(text), untrusted_prefixes):
        return EvidenceRef(raw, "path", _normalise(text), TRUST_UNTRUSTED)
    if kind == "pytest":
        return EvidenceRef(raw, kind, target, TRUST_TRUSTED)
    if kind == "path":
        trusted = target in set(artifact_paths)
        return EvidenceRef(raw, kind, target, TRUST_TRUSTED if trusted else TRUST_UNRESOLVED)
    if kind in {"tool-run", "knowledge"}:
        return EvidenceRef(raw, kind, target, TRUST_TRUSTED)
    return EvidenceRef(raw, kind, target, TRUST_UNRESOLVED)


def _is_untrusted(path: str, prefixes: Sequence[str]) -> bool:
    normalised = _normalise(path)
    return any(
        normalised == _normalise(prefix) or normalised.startswith(_normalise(prefix) + "/")
        for prefix in prefixes
        if prefix.strip()
    )


def ran_test_targets(verifier_results: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """``target → passed`` for every pytest run the ``code_test`` layer recorded."""

    targets: dict[str, bool] = {}
    for layer in verifier_results:
        if layer.get("layer") != "code_test":
            continue
        detail = layer.get("detail") or {}
        for run in detail.get("runs", []) if isinstance(detail, Mapping) else []:
            if not isinstance(run, Mapping):
                continue
            target = run.get("target")
            key = "" if target is None else _normalise(str(target))
            targets[key] = bool(run.get("passed")) and targets.get(key, True)
    return targets


def covering_target(path: str, ran: Mapping[str, bool]) -> str | None:
    """The passed run target that covers ``path``: the exact target or a directory that
    contains it.  A whole-tree run (empty target) covers **nothing** (D4-2'): it only
    supports the Mission-level judgment, never a claim (ORCH §12.4)."""

    normalised = _normalise(path)
    best: str | None = None
    for target, passed in ran.items():
        if not passed or target == "":
            continue
        if target == normalised or normalised.startswith(target + "/"):
            if best is None or len(target) > len(best):
                best = target
    return best


@dataclass(frozen=True, slots=True)
class ClaimGrade:
    claim_id: str
    status: ClaimStatus
    basis: Mapping[str, Any]
    evidence: tuple[EvidenceRef, ...]

    @property
    def evidence_trust(self) -> tuple[str, ...]:
        return tuple(sorted({ref.trust for ref in self.evidence}))

    def to_json(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "status": str(self.status),
            "basis": dict(self.basis),
            "evidence": [ref.to_json() for ref in self.evidence],
        }


def grade_claim(
    claim_id: str,
    evidence: Sequence[str],
    *,
    verifier_results: Sequence[Mapping[str, Any]],
    artifact_paths: Sequence[str],
    untrusted_prefixes: Sequence[str],
) -> ClaimGrade:
    ran = ran_test_targets(verifier_results)
    refs = tuple(
        parse_evidence(
            item,
            artifact_paths=artifact_paths,
            untrusted_prefixes=untrusted_prefixes,
            ran_targets=ran,
        )
        for item in evidence
    )
    for ref in refs:
        if ref.kind == "pytest" and ref.trust == TRUST_TRUSTED:
            target = covering_target(ref.target, ran)
            if target is not None:
                return ClaimGrade(
                    claim_id,
                    ClaimStatus.VERIFIED,
                    {"layer": "code_test", "target": target, "evidence": ref.raw},
                    refs,
                )
    if any(ref.trust == TRUST_TRUSTED for ref in refs):
        return ClaimGrade(
            claim_id,
            ClaimStatus.SUPPORTED,
            {"layer": "rule_check", "reason": "cites trusted evidence but no test run covers it"},
            refs,
        )
    return ClaimGrade(
        claim_id,
        ClaimStatus.UNDER_REVIEW,
        {
            "grade": "unsupported",
            "reason": "no trusted evidence cited"
            if not refs
            else "cited evidence is untrusted or unresolved",
        },
        refs,
    )


__all__ = (
    "TRUST_TRUSTED",
    "TRUST_UNRESOLVED",
    "TRUST_UNTRUSTED",
    "ClaimGrade",
    "EvidenceRef",
    "covering_target",
    "grade_claim",
    "parse_evidence",
    "ran_test_targets",
)
