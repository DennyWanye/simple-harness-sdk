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

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..artifacts.paths import normalise_workspace_path, under_prefix
from ..contracts import ClaimProposal, ClaimStatus
from ..contracts.models import canonical_json, sha256_hex

if TYPE_CHECKING:
    from ..contracts.assessments import CriterionAssessmentV1
    from ..governance.domains import DomainProfileV1

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
    return normalise_workspace_path(path)


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
    return under_prefix(path, tuple(prefixes))


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
    domain: DomainProfileV1 | None = None,
    proposal: ClaimProposal | None = None,
    assessments: Sequence[CriterionAssessmentV1] = (),
) -> ClaimGrade:
    if domain is not None and domain.id == "doc-research-v1":
        return _grade_document(claim_id, proposal, assessments, verifier_results)
    return _legacy_grade_claim(
        claim_id,
        evidence,
        verifier_results=verifier_results,
        artifact_paths=artifact_paths,
        untrusted_prefixes=untrusted_prefixes,
    )


def _legacy_grade_claim(
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


def normalise_quote(text: str) -> str:
    """Same NFC/whitespace equivalence as citation resolution; never NFKC."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def system_attribution(basis: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Only system grading provenance marks source material in downstream contexts.

    Callers pass the system-owned verifier/basis, never the model's ``type``.
    """
    item = basis.get("attribution")
    if (
        basis.get("system_domain") == "doc-research-v1"
        and basis.get("adapter") in {"citation_integrity@v1", "citation_integrity@v2"}
        and basis.get("grade") == "verified"
        and isinstance(item, Mapping)
        and item.get("source_trust") == TRUST_UNTRUSTED
        and isinstance(item.get("identity"), (list, tuple))
        and len(item["identity"]) == 5
    ):
        return item
    return None


def _grade_document(
    claim_id: str,
    proposal: ClaimProposal | None,
    assessments: Sequence[CriterionAssessmentV1],
    verifier_results: Sequence[Mapping[str, Any]],
) -> ClaimGrade:
    basis: dict[str, Any] = {
        "layer": "rule_check",
        "adapter": "citation_integrity@v1",
        "system_domain": "doc-research-v1",
        "grade": "unsupported",
        "scope_limited_to_source": True,
    }
    rows = [a for a in assessments if a.claim_id == claim_id]
    failures: list[str] = []
    # Failure detail is diagnostic only. It cannot upgrade a claim. In particular,
    # Critic text and a caller's alleged code_test PASS never enter document grading.
    for layer in verifier_results:
        if layer.get("layer") != "rule_check":
            continue
        detail = layer.get("detail", {})
        if isinstance(detail, Mapping):
            for entry in detail.get("evidence_resolutions", ()):
                if isinstance(entry, Mapping) and entry.get("claim_id") == claim_id:
                    resolution = entry.get("resolution", {})
                    if isinstance(resolution, Mapping) and resolution.get("status") != "resolved":
                        failures.append(str(resolution.get("status", "unresolved")))
    if failures:
        basis["failure_codes"] = sorted(set(failures))
    if proposal is None or not proposal.citations or not rows:
        basis["reason"] = "no validated citation assessment for this claim"
        return ClaimGrade(claim_id, ClaimStatus.UNDER_REVIEW, basis, ())
    claim_hash = sha256_hex(proposal.to_json())
    if any(
        a.verifier_adapter_id != "citation_integrity"
        or a.version not in {"1", "2"}
        or a.verdict not in ({"PASS", "INCONCLUSIVE"} if a.version == "2" else {"PASS"})
        or a.provenance.get("producer") != f"citation_integrity@v{a.version}"
        or a.provenance.get("claim_hash") != claim_hash
        for a in rows
    ):
        basis["reason"] = "assessment is not a deterministic PASS for this claim"
        return ClaimGrade(claim_id, ClaimStatus.UNDER_REVIEW, basis, ())
    if len({a.version for a in rows}) != 1:
        basis["reason"] = "mixed assessment versions cannot grade one claim"
        return ClaimGrade(claim_id, ClaimStatus.UNDER_REVIEW, basis, ())
    basis["adapter"] = f"citation_integrity@v{rows[0].version}"
    expected = sorted(canonical_json(c.to_json()) for c in proposal.citations)
    # Every linked criterion must have evaluated the complete citation set. Choosing
    # one good reference from a partially failed set would launder the other quote.
    for row in rows:
        row_refs = row.to_json()["evidence_refs"]
        if sorted(canonical_json(item.get("ref")) for item in row_refs) != expected or any(
            item.get("status") != "resolved" for item in row_refs
        ):
            basis["reason"] = "citation set is incomplete or unresolved"
            return ClaimGrade(claim_id, ClaimStatus.UNDER_REVIEW, basis, ())
    resolved = sorted(rows[0].to_json()["evidence_refs"], key=lambda item: canonical_json(item))
    refs = tuple(
        EvidenceRef(
            f"source:{item['ref']['path']}@{item['source_version']}",
            "source",
            str(item["ref"]["path"]),
            TRUST_UNTRUSTED,
        )
        for item in resolved
    )
    basis["assessment_receipts"] = sorted(a.receipt_id for a in rows)
    basis["evidence_refs"] = [dict(item) for item in resolved]
    if any(a.verdict == "INCONCLUSIVE" for a in rows):
        basis.update(
            grade="insufficient_evidence",
            reason="resolved sources do not bind this claim to every candidate criterion",
            inconclusive_criteria=sorted(
                {a.criterion_id for a in rows if a.verdict == "INCONCLUSIVE"}
            ),
        )
        return ClaimGrade(claim_id, ClaimStatus.UNDER_REVIEW, basis, refs)
    matching = [
        item
        for item in resolved
        if normalise_quote(proposal.content) == normalise_quote(str(item["ref"]["quote"]))
    ]
    if not matching:
        basis.update(grade="supported", type_downgraded=proposal.type == "attribution")
        basis["reason"] = "citations resolve, but a statement is not a verified world fact"
        return ClaimGrade(claim_id, ClaimStatus.SUPPORTED, basis, refs)
    primary = min(
        matching,
        key=lambda item: (
            str(item["source_version"]),
            str(item["ref"]["path"]),
            item["locator"]["start_line"],
            item["locator"]["end_line"],
            normalise_quote(str(item["ref"]["quote"])),
        ),
    )
    path, version = str(primary["ref"]["path"]), str(primary["source_version"])
    start, end = primary["locator"]["start_line"], primary["locator"]["end_line"]
    quote = str(primary["ref"]["quote"])
    key = f"attribution:{version}:{start}-{end}"
    basis.update(
        grade="verified",
        type_downgraded=False,
        key_downgraded=proposal.key != key,
        attribution={
            "content": f"《{path}》@{version[:8]} #L{start}-L{end} 记载：「{quote}」",
            "key": key,
            "stance": "affirms",
            "type": "attribution",
            "identity": [version, path, start, end, normalise_quote(quote)],
            "primary": dict(primary),
            "source_trust": TRUST_UNTRUSTED,
        },
    )
    return ClaimGrade(claim_id, ClaimStatus.VERIFIED, basis, refs)


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
    "normalise_quote",
    "system_attribution",
)
