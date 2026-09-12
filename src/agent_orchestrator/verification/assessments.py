# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen claim/criterion bindings and deterministic citation assessment receipts.

Only the resolver reads source bytes. Acceptance and reuse validate the same durable
description; checksums identify content, not signatures or protection from DB admins.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..artifacts.store import ArtifactStoreError, read_verified
from ..contracts import (
    Artifact,
    Attempt,
    ContractError,
    CriterionAssessmentV1,
    Mission,
    ResultEnvelope,
    Task,
    ids,
)
from ..contracts.assessments import content_hash, freeze_json, required_text, thaw_json
from ..contracts.models import sha256_hex
from ..governance.domains import DOC_DOMAIN, DomainProfileV1, criterion_kind
from . import adapters
from .deterministic_checks import ERROR, FAIL, PASS, LayerResult
from .evidence_resolver import EvidenceResolver

if TYPE_CHECKING:
    from ..storage.store import Store

ASSESSMENT_PRODUCER = "citation_integrity@v1"
_CONTRACT_TEXT = ("task_id", "kind", "goal", "rationale")
_CONTRACT_LIST = ("success_criteria", "verification_policy", "outputs")


def normalise_literal(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _contract(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("assessment frozen task_contract must be an object")
    raw = {
        **value,
        "task_id": value.get("task_id", value.get("id")),
        "kind": value.get("kind", "work"),
    }
    if value.get("task_id") is not None and value.get("id") not in {None, value["task_id"]}:
        raise ContractError("assessment task_contract has conflicting identities")
    result: dict[str, Any] = {}
    for name in _CONTRACT_TEXT:
        item = raw.get(name)
        if not isinstance(item, str) or (name != "rationale" and not item.strip()):
            raise ContractError(f"assessment frozen task_contract is missing {name}")
        result[name] = item
    for name in _CONTRACT_LIST:
        items = raw.get(name)
        if not isinstance(items, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise ContractError(f"assessment frozen task_contract has invalid {name}")
        result[name] = list(items)
    return result


def task_contract_revision(contract: Mapping[str, Any]) -> str:
    return sha256_hex(_contract(contract))


def criterion_id(revision: str, ordinal: int, text: str) -> str:
    content_hash(revision, "task_contract_revision")
    if type(ordinal) is not int or ordinal < 1:
        raise ContractError("criterion ordinal must be a positive integer")
    required_text(text, "criterion")
    return "criterion-" + sha256_hex({"revision": revision, "ordinal": ordinal, "text": text})


def mission_contract_revision(mission: Mission) -> str:
    criteria = mission.success_criteria
    if not isinstance(criteria, (list, tuple)) or any(
        not isinstance(text, str) or not text.strip() for text in criteria
    ):
        raise ContractError("invalid original Mission criteria")
    if len(set(criteria)) != len(criteria):
        raise ContractError("original Mission criteria must not contain duplicates")
    return sha256_hex(
        {
            "scope": "mission",
            "mission_id": required_text(mission.id, "mission_id"),
            "goal": required_text(mission.goal, "mission goal"),
            "success_criteria": list(criteria),
        }
    )


def mission_criterion_catalog(mission: Mission) -> tuple[dict[str, Any], ...]:
    revision = mission_contract_revision(mission)
    return tuple(
        {
            "criterion_id": criterion_id(revision, ordinal, text),
            "ordinal": ordinal,
            "text": text,
            "kind": criterion_kind(text),
        }
        for ordinal, text in enumerate(mission.success_criteria, 1)
    )


@dataclass(frozen=True, slots=True)
class AssessmentBindingV1:
    tenant_id: str
    mission_id: str
    task_id: str
    attempt_id: str
    result_id: str
    task_contract: Mapping[str, Any]
    output_hash: str
    source_versions: Mapping[str, str]
    source_roots: tuple[str, ...]
    claim_revisions: Mapping[str, int]
    envelope: ResultEnvelope
    mission_contract_revision: str | None = None
    mission_criteria: tuple[Mapping[str, Any], ...] = ()
    check_spec_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("task_contract", "source_versions", "claim_revisions"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        object.__setattr__(self, "mission_criteria", freeze_json(self.mission_criteria))
        object.__setattr__(self, "check_spec_ids", tuple(self.check_spec_ids))

    @property
    def task_contract_revision(self) -> str:
        return task_contract_revision(self.task_contract)

    @property
    def criteria(self) -> tuple[Mapping[str, Any], ...]:
        revision = self.task_contract_revision
        return tuple(
            freeze_json(
                {
                    "id": criterion_id(revision, ordinal, text),
                    "ordinal": ordinal,
                    "text": text,
                    "kind": criterion_kind(text),
                }
            )
            for ordinal, text in enumerate(self.task_contract["success_criteria"], 1)
        )

    def to_json(self) -> dict[str, Any]:
        result = {
            "schema": 1,
            "tenant_id": self.tenant_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "result_id": self.result_id,
            "task_contract": thaw_json(self.task_contract),
            "task_contract_revision": self.task_contract_revision,
            "output_hash": self.output_hash,
            "source_versions": thaw_json(self.source_versions),
            "source_roots": list(self.source_roots),
            "claim_revisions": thaw_json(self.claim_revisions),
            "envelope": self.envelope.to_json(),
        }
        if self.check_spec_ids:
            result.update(
                mission_contract_revision=self.mission_contract_revision,
                mission_criteria=thaw_json(self.mission_criteria),
                check_spec_ids=list(self.check_spec_ids),
            )
        return result

    @property
    def binding_hash(self) -> str:
        return sha256_hex(self.to_json())


def _frozen_contract(config: Mapping[str, Any]) -> dict[str, Any]:
    if "task_contract" in config:
        return _contract(config["task_contract"])
    message = config.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise ContractError("assessment intent has no frozen task_contract")
    try:
        package = json.loads(content)
    except (ValueError, TypeError):
        package = None
    if isinstance(package, Mapping) and "task_contract" in package:
        return _contract(package["task_contract"])
    sections = re.findall(
        r"^## task_contract\s*\n(.*?)(?=^## |\Z)", content, flags=re.MULTILINE | re.DOTALL
    )
    if len(sections) == 1:
        try:
            return _contract(json.loads(sections[0]))
        except (ValueError, TypeError) as error:
            raise ContractError("assessment frozen task_contract section is invalid") from error
    raise ContractError("assessment intent has no unique frozen task_contract")


def assessment_binding_for(
    store: Store,
    *,
    task: Task,
    attempt: Attempt,
    envelope: ResultEnvelope,
    artifacts: Sequence[Artifact],
) -> AssessmentBindingV1:
    return _assessment_binding_for(
        store, task=task, attempt=attempt, envelope=envelope, artifacts=artifacts
    )


def _assessment_binding_for(
    store: Store,
    *,
    task: Task,
    attempt: Attempt,
    envelope: ResultEnvelope,
    artifacts: Sequence[Artifact],
    historical_revisions: Mapping[str, int] | None = None,
) -> AssessmentBindingV1:
    """Read authoritative frozen identities; never write or consult active sources."""
    mission = store.get_mission(attempt.mission_id)
    intent = store.get_intent_for_subject(attempt.id)
    if mission is None or intent is None:
        raise ContractError("assessment requires a durable Mission and Attempt intent")
    if (
        task.mission_id != attempt.mission_id
        or task.id != attempt.task_id
        or envelope.task_id != task.id
        or envelope.attempt_id != attempt.id
        or envelope.mission_id not in {"", mission.id}
        or intent.subject_id != attempt.id
        or intent.mission_id != mission.id
        or intent.kind != "attempt"
    ):
        raise ContractError("assessment binding identity mismatch")
    contract = _frozen_contract(intent.config)
    current = {name: getattr(task, name) for name in (*_CONTRACT_TEXT[1:], *_CONTRACT_LIST)}
    current["task_id"] = task.id
    revision = task_contract_revision(contract)
    if revision != task_contract_revision(current):
        raise ContractError("assessment frozen Task contract differs from current semantics")
    if intent.config.get("task_contract_revision", revision) != revision:
        raise ContractError("assessment frozen Task contract revision mismatch")
    versions = intent.config.get("source_versions", {})
    roots = intent.config.get("source_roots", ())
    if not isinstance(versions, Mapping) or not isinstance(roots, (list, tuple)):
        raise ContractError("assessment frozen sources have invalid shape")
    for path, version in versions.items():
        required_text(path, "source path")
        content_hash(version, "source version")
    for root in roots:
        required_text(root, "source root")
    specs = intent.config.get("check_spec_ids", ())
    mission_revision = None
    mission_criteria: tuple[Mapping[str, Any], ...] = ()
    if (
        not isinstance(specs, (list, tuple))
        or any(not isinstance(spec, str) or not spec.strip() for spec in specs)
        or len(set(specs)) != len(specs)
    ):
        raise ContractError("invalid frozen check specifications")
    frozen = store.get_mission_domain(mission.id)
    try:
        domain = DomainProfileV1.from_json(frozen["json"]) if frozen is not None else None
        if (
            frozen is not None
            and domain is not None
            and (domain.id, domain.version)
            != (
                frozen["domain_id"],
                frozen["domain_version"],
            )
        ):
            raise ValueError("domain identity mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError(f"invalid frozen assessment domain: {error}") from error
    if domain is not None and (domain.id, domain.version) == (DOC_DOMAIN, "3"):
        if tuple(specs) != tuple(sorted(domain.adapters.values())):
            raise ContractError("DOC3 requires its exact frozen check specifications")
    elif any(
        key in intent.config
        for key in ("check_spec_ids", "mission_contract_revision", "mission_criteria")
    ):
        raise ContractError("legacy domain cannot acquire D check specifications")
    if specs:
        mission_revision = mission_contract_revision(mission)
        mission_criteria = mission_criterion_catalog(mission)
        if intent.config.get("mission_contract_revision") != mission_revision or intent.config.get(
            "mission_criteria"
        ) != list(mission_criteria):
            raise ContractError("frozen Mission criterion directory or revision mismatch")
    elif any(key in intent.config for key in ("mission_contract_revision", "mission_criteria")):
        raise ContractError("Mission directory has no frozen check specification")
    claims = {}
    for ordinal, proposal in enumerate(envelope.claims, 1):
        cid = ids.claim_id(envelope.id, ordinal)
        claim = store.get_claim(cid)
        if (
            claim is None
            or claim.mission_id != mission.id
            or claim.result_id != envelope.id
            or claim.source_task != task.id
            or claim.source_attempt != attempt.id
            or (historical_revisions is None and claim.content != proposal.content)
            or type(claim.version) is not int
            or claim.version < 1
        ):
            raise ContractError("assessment claim identity, content or revision mismatch")
        claim_revision = (
            claim.version if historical_revisions is None else historical_revisions.get(cid)
        )
        if type(claim_revision) is not int or not 1 <= claim_revision <= claim.version:
            raise ContractError("invalid original assessment claim revision")
        claims[cid] = claim_revision
    if historical_revisions is not None and set(historical_revisions) != set(claims):
        raise ContractError("original claim revision directory is incomplete")
    artifact_rows: list[dict[str, str]] = []
    for artifact in artifacts:
        if (
            artifact.mission_id != mission.id
            or artifact.task_id != task.id
            or artifact.attempt_id != attempt.id
        ):
            raise ContractError("assessment artifact identity mismatch")
        artifact_rows.append(
            {
                "id": artifact.id,
                "path": artifact.path,
                "content_hash": content_hash(artifact.content_hash, "artifact hash"),
            }
        )
    if len({a["id"] for a in artifact_rows}) != len(artifact_rows) or len(
        {a["path"] for a in artifact_rows}
    ) != len(artifact_rows):
        raise ContractError("assessment duplicate artifact identity")
    if not set(envelope.artifacts).issubset({a["path"] for a in artifact_rows}):
        raise ContractError("assessment artifact catalogue does not cover output")
    sorted_artifacts = sorted(artifact_rows, key=lambda a: (a["id"], a["path"], a["content_hash"]))
    binding = AssessmentBindingV1(
        tenant_id=mission.tenant_id,
        mission_id=mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        result_id=envelope.id,
        task_contract=contract,
        output_hash=sha256_hex(
            {
                "envelope": envelope.to_json(),
                "artifacts": sorted_artifacts,
            }
        ),
        source_versions=dict(versions),
        source_roots=tuple(roots),
        claim_revisions=claims,
        envelope=ResultEnvelope.from_json(envelope.to_json()),
        mission_contract_revision=mission_revision,
        mission_criteria=mission_criteria,
        check_spec_ids=tuple(specs),
    )
    if specs:
        _check_candidate_ids(binding)
    return binding


def _check_candidate_ids(binding: AssessmentBindingV1) -> None:
    task_ids = {c["id"] for c in binding.criteria}
    mission_ids = {c["criterion_id"] for c in binding.mission_criteria}
    for proposal in binding.envelope.claims:
        if (
            not set(proposal.criterion_ids) <= task_ids
            or not set(proposal.mission_criterion_ids) <= mission_ids
        ):
            raise ContractError("candidate IDs are outside their frozen criterion catalogue")
    for item in binding.envelope.limitations:
        if item.criterion_id not in task_ids:
            raise ContractError("limitations must reference a frozen Task criterion")


def accepted_assessments_for(
    store: Store, *, task: Task
) -> tuple[AssessmentBindingV1, tuple[CriterionAssessmentV1, ...]]:
    """Read accepted original evidence without regrading current historical claims."""
    stored = store.get_result(task.accepted_result_id or "")
    if stored is None or stored.verification_state != "DONE" or stored.verdict != PASS:
        raise ContractError("assessment reader requires the Task's accepted DONE/PASS result")
    envelope = stored.envelope
    if envelope.task_id != task.id or envelope.mission_id != task.mission_id:
        raise ContractError("accepted assessment result belongs to another Task/Mission")
    attempt = store.get_attempt(envelope.attempt_id)
    if attempt is None:
        raise ContractError("accepted assessment Attempt is unavailable")
    rules = [row for row in store.list_verifications(envelope.id) if row["layer"] == "rule_check"]
    if len(rules) != 1:
        raise ContractError("accepted assessment needs one real rule_check record")
    original = rules[0]["detail"].get("assessment_binding")
    if not isinstance(original, Mapping) or not isinstance(
        original.get("claim_revisions"), Mapping
    ):
        raise ContractError("accepted original binding is unavailable")
    artifacts = []
    for aid in stored.artifacts:
        artifact = store.get_artifact(aid)
        if artifact is None:
            raise ContractError("accepted assessment artifact is unavailable")
        try:
            read_verified(artifact)
        except ArtifactStoreError as error:
            raise ContractError(f"accepted artifact invalid: {error.reason}") from error
        artifacts.append(artifact)
    binding = _assessment_binding_for(
        store,
        task=task,
        attempt=attempt,
        envelope=envelope,
        artifacts=artifacts,
        historical_revisions=original["claim_revisions"],
    )
    rows = validated_assessments(rules[0], binding=binding)
    persisted = store.list_criterion_assessments(task.mission_id, result_id=envelope.id)
    expected = sorted(sha256_hex(row.to_json()) for row in rows)
    if sorted(sha256_hex(row) for row in persisted) != expected:
        raise ContractError("accepted assessment receipt table differs from the validated record")
    return binding, rows


def _layer(value: object) -> LayerResult:
    if isinstance(value, LayerResult):
        return value
    if not isinstance(value, Mapping) or not isinstance(value.get("detail"), Mapping):
        raise ContractError("assessment needs a recorded layer with detail")
    if not isinstance(value.get("layer"), str) or not isinstance(value.get("status"), str):
        raise ContractError("assessment recorded layer has invalid identity/status")
    return LayerResult(
        value["layer"], value["status"], str(value.get("summary", "")), value["detail"]
    )


def _evaluation(
    binding: AssessmentBindingV1,
    resolutions: Sequence[Mapping[str, Any]],
    structural: LayerResult,
) -> LayerResult:
    """Pure evaluation: no source reader, source bytes, tools, model or workspace."""
    if structural.layer != "rule_check":
        raise ContractError("citation integrity must compose with rule_check")
    by_claim: dict[str, list[dict[str, Any]]] = {}
    proposals_by_id = {
        ids.claim_id(binding.result_id, ordinal): proposal
        for ordinal, proposal in enumerate(binding.envelope.claims, 1)
    }
    expected = [
        (ids.claim_id(binding.result_id, i), j, citation)
        for i, claim in enumerate(binding.envelope.claims, 1)
        for j, citation in enumerate(claim.citations, 1)
    ]
    if len(resolutions) != len(expected):
        raise ContractError("assessment resolution catalogue is incomplete")
    for item, (cid, ordinal, citation) in zip(resolutions, expected, strict=True):
        if not isinstance(item, Mapping) or set(item) != {
            "claim_id",
            "citation_index",
            "resolution",
        }:
            raise ContractError("assessment resolution entry is malformed")
        if (
            item["claim_id"] != cid
            or type(item["citation_index"]) is not int
            or item["citation_index"] != ordinal
        ):
            raise ContractError("assessment resolution belongs to another claim/citation")
        ref = item["resolution"]
        if not isinstance(ref, Mapping):
            raise ContractError("assessment resolution must be an object")
        ref = thaw_json(freeze_json(ref))
        if ref.get("status") == "resolved":
            locator = ref.get("locator")
            display = ref.get("display_block")
            if (
                ref.get("schema") != 1
                or ref.get("ref") != citation.to_json()
                or ref.get("target") != citation.path
                or ref.get("kind") != "source"
                or ref.get("source_version") != citation.version
                or binding.source_versions.get(citation.path) != citation.version
                or ref.get("tenant_id") != binding.tenant_id
                or ref.get("mission_id") != binding.mission_id
                or ref.get("source_trust") != "untrusted_external"
                or not isinstance(locator, Mapping)
                or set(locator) != {"start_line", "end_line"}
                or any(type(locator.get(k)) is not int for k in ("start_line", "end_line"))
                or not citation.start_line
                <= locator["start_line"]
                <= locator["end_line"]
                <= citation.end_line
                or not isinstance(display, Mapping)
            ):
                raise ContractError(
                    "assessment resolution does not match the submitted frozen citation"
                )
            if not any(
                citation.path == root.rstrip("/")
                or citation.path.startswith(root.rstrip("/") + "/")
                for root in binding.source_roots
            ):
                raise ContractError("assessment resolved citation is outside frozen roots")
        elif ref.get("status") not in {
            "not_found",
            "unreadable",
            "stale_source",
            "span_out_of_range",
            "quote_mismatch",
            "quote_ambiguous",
            "quote_not_whole_unit",
        }:
            raise ContractError("assessment resolution has unknown status")
        by_claim.setdefault(cid, []).append(ref)
    all_resolved = all(item["resolution"]["status"] == "resolved" for item in resolutions)
    assessments = []
    verdicts = []
    for criterion in binding.criteria:
        kind, text = criterion["kind"], criterion["text"]
        structural_kind = kind in {"file", "action", "arbitration"}
        structural_scopes = {
            "file": "file_exists",
            "action": "action_candidate_schema_deployment_charter",
            "arbitration": "arbitration_claim_structure",
        }
        scope = (
            structural_scopes[kind]
            if structural_kind
            else "source_path"
            if kind == "cite"
            else "literal"
        )
        linked = []
        reasons = []
        if structural_kind:
            verdict = structural.status
        else:
            for ordinal, proposal in enumerate(binding.envelope.claims, 1):
                cid = ids.claim_id(binding.result_id, ordinal)
                matches = (
                    any(c.path == text.removeprefix("cite:") for c in proposal.citations)
                    if kind == "cite"
                    else kind == "free"
                    and normalise_literal(proposal.content) == normalise_literal(text)
                )
                if matches:
                    linked.append(cid)
            if not linked:
                reasons.append("no_content_binding")
            for cid in linked:
                refs = by_claim.get(cid, [])
                if not refs:
                    reasons.append("missing_citation")
                reasons.extend(ref["status"] for ref in refs if ref["status"] != "resolved")
            verdict = FAIL if reasons else PASS
            if structural.status == PASS and verdict == PASS:
                for cid in linked:
                    refs = sorted(
                        by_claim[cid],
                        key=lambda r: (
                            r["source_version"],
                            r["target"],
                            r["locator"]["start_line"],
                            r["locator"]["end_line"],
                            normalise_literal(r["ref"]["quote"]),
                            sha256_hex(r),
                        ),
                    )
                    proposal = proposals_by_id[cid]
                    assessment = CriterionAssessmentV1.create(
                        criterion_id=criterion["id"],
                        task_contract_revision=binding.task_contract_revision,
                        claim_id=cid,
                        claim_revision=binding.claim_revisions[cid],
                        output_ref=binding.result_id,
                        output_hash=binding.output_hash,
                        evidence_refs=refs,
                        source_versions={r["target"]: r["source_version"] for r in refs},
                        verifier_adapter_id="citation_integrity",
                        version="1",
                        checked_scope={
                            "kind": "source_citation",
                            "binding": scope,
                            "criterion": text,
                        },
                        verdict=PASS,
                        provenance={
                            "schema": 1,
                            "producer": ASSESSMENT_PRODUCER,
                            "binding_hash": binding.binding_hash,
                            "tenant_id": binding.tenant_id,
                            "mission_id": binding.mission_id,
                            "task_id": binding.task_id,
                            "attempt_id": binding.attempt_id,
                            "claim_hash": sha256_hex(proposal.to_json()),
                        },
                    )
                    assessments.append(assessment.to_json())
        verdicts.append(
            {
                "criterion_id": criterion["id"],
                "kind": kind,
                "scope": scope,
                "verdict": verdict,
                "claim_ids": linked,
                "reasons": sorted(set(reasons)),
            }
        )
    failed = not all_resolved or any(v["verdict"] != PASS for v in verdicts)
    status = structural.status if structural.status != PASS else FAIL if failed else PASS
    detail = {
        **dict(structural.detail),
        "assessment_schema": 1,
        "assessment_binding": binding.to_json(),
        "structural_result": structural.to_json(),
        "evidence_resolutions": [thaw_json(freeze_json(item)) for item in resolutions],
        "criterion_verdicts": verdicts,
        "criterion_assessments": assessments,
    }
    return LayerResult(
        "rule_check",
        status,
        "citation integrity checked"
        if status == PASS
        else "citation integrity or structural checks failed",
        detail,
    )


def _v2_parts(
    binding: AssessmentBindingV1,
    resolutions: Sequence[Mapping[str, Any]],
    structural: LayerResult,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Deterministic eligibility and complete limitations; never run the external phase."""
    if binding.check_spec_ids != adapters.DOCUMENT_CHECKS or any(
        spec not in adapters.CHECK_SPECS for spec in binding.check_spec_ids
    ):
        raise ContractError("unknown or missing required document check specification")
    _check_candidate_ids(binding)
    # Reuse v1's exact resolution/identity validation and structural directory. Its
    # published producer and receipts remain untouched when the binding selects v1.
    baseline = _evaluation(binding, resolutions, structural)
    detail = dict(baseline.detail)
    by_claim: dict[str, list[Mapping[str, Any]]] = {}
    for item in resolutions:
        by_claim.setdefault(item["claim_id"], []).append(item["resolution"])
    rows: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    hard: list[str] = []
    if structural.status != PASS:
        hard.append("structural_failure")
    if any(item["resolution"]["status"] != "resolved" for item in resolutions):
        hard.append("citation_not_resolved")
    for criterion, old in zip(binding.criteria, detail["criterion_verdicts"], strict=True):
        kind, text = criterion["kind"], criterion["text"]
        if kind in {"file", "action", "arbitration"}:
            verdicts.append(old)
            continue
        linked: list[str] = []
        reasons: list[str] = []
        pair_verdicts: list[str] = []
        for ordinal, proposal in enumerate(binding.envelope.claims, 1):
            cid = ids.claim_id(binding.result_id, ordinal)
            matches = (
                any(c.path == text.removeprefix("cite:") for c in proposal.citations)
                if kind == "cite"
                else kind == "free"
                and normalise_literal(proposal.content) == normalise_literal(text)
            )
            candidate = criterion["id"] in proposal.criterion_ids
            if not matches and not candidate:
                continue
            linked.append(cid)
            refs = by_claim.get(cid, [])
            if not refs:
                reasons.append("missing_citation")
                continue
            if any(ref["status"] != "resolved" for ref in refs):
                reasons.append("citation_not_resolved")
                continue
            verdict = PASS if matches else "INCONCLUSIVE"
            pair_verdicts.append(verdict)
            if structural.status != PASS:
                continue
            ordered = sorted(
                refs,
                key=lambda r: (
                    r["source_version"],
                    r["target"],
                    r["locator"]["start_line"],
                    r["locator"]["end_line"],
                    normalise_literal(r["ref"]["quote"]),
                    sha256_hex(r),
                ),
            )
            rows.append(
                CriterionAssessmentV1.create(
                    criterion_id=criterion["id"],
                    task_contract_revision=binding.task_contract_revision,
                    claim_id=cid,
                    claim_revision=binding.claim_revisions[cid],
                    output_ref=binding.result_id,
                    output_hash=binding.output_hash,
                    evidence_refs=ordered,
                    source_versions={r["target"]: r["source_version"] for r in ordered},
                    verifier_adapter_id="citation_integrity",
                    version="2",
                    checked_scope={
                        "kind": "source_citation",
                        "catalog": "task",
                        "criterion": text,
                        "binding": ("source_path" if kind == "cite" else "literal")
                        if matches
                        else "candidate",
                    },
                    verdict=verdict,
                    provenance={
                        "schema": 1,
                        "producer": "citation_integrity@v2",
                        "binding_hash": binding.binding_hash,
                        "tenant_id": binding.tenant_id,
                        "mission_id": binding.mission_id,
                        "task_id": binding.task_id,
                        "attempt_id": binding.attempt_id,
                        "claim_hash": sha256_hex(proposal.to_json()),
                        "mission_contract_revision": binding.mission_contract_revision,
                    },
                ).to_json()
            )
        if not linked:
            reasons.append("no_content_binding_or_candidate")
        verdicts.append(
            {
                "criterion_id": criterion["id"],
                "kind": kind,
                "scope": old["scope"],
                "verdict": FAIL
                if reasons
                else "INCONCLUSIVE"
                if "INCONCLUSIVE" in pair_verdicts
                else PASS,
                "claim_ids": linked,
                "reasons": sorted(set(reasons)),
            }
        )
        hard.extend(reasons)
    eligible = [row for row in rows if row["verdict"] == "INCONCLUSIVE"]
    required = sorted({(row["criterion_id"], row["claim_id"]) for row in eligible})
    provided = {
        (
            item.criterion_id,
            ids.claim_id(binding.result_id, int(item.claim_id.split(":")[1])),
        ): item.missing
        for item in binding.envelope.limitations
    }
    missing = [pair for pair in required if pair not in provided]
    if missing:
        hard.append("missing_limitations")
    detail.update(
        criterion_verdicts=verdicts,
        criterion_assessments=rows,
        limitations_check={
            "required": [{"criterion_id": cid, "claim_id": claim} for cid, claim in required],
            "provided": [
                {"criterion_id": cid, "claim_id": claim, "missing": value}
                for (cid, claim), value in sorted(provided.items())
            ],
            "missing": [{"criterion_id": cid, "claim_id": claim} for cid, claim in missing],
        },
        hard_failures=sorted(set(hard)),
    )
    return detail, eligible, sorted(set(hard))


def _evaluation_v2(
    binding: AssessmentBindingV1,
    resolutions: Sequence[Mapping[str, Any]],
    structural: LayerResult,
    external: Mapping[str, Any],
) -> LayerResult:
    detail, eligible, hard = _v2_parts(binding, resolutions, structural)
    dispatched = eligible if not hard else []
    expected = adapters.coverage_record(
        binding=binding, eligible=dispatched, verdict=external.get("verdict")
    )
    if dict(external) != expected:
        raise ContractError("external phase receipt or frozen input differs")
    status = structural.status if structural.status != PASS else FAIL if hard else PASS
    if not hard and eligible and external["verdict"] in {FAIL, "NEEDS_HUMAN"}:
        status = external["verdict"]
    detail["external_check"] = expected
    return LayerResult("rule_check", status, "document citation and limitations checked", detail)


def citation_integrity(
    *,
    binding: AssessmentBindingV1,
    envelope: ResultEnvelope,
    resolver: EvidenceResolver,
    structural_result: LayerResult,
) -> LayerResult:
    if envelope.to_json() != binding.envelope.to_json():
        raise ContractError("assessment producer output differs from frozen binding")
    resolutions = []
    for i, claim in enumerate(envelope.claims, 1):
        for j, citation in enumerate(claim.citations, 1):
            result = resolver.resolve(
                citation,
                tenant_id=binding.tenant_id,
                mission_id=binding.mission_id,
                source_versions=binding.source_versions,
                source_roots=binding.source_roots,
            )
            resolutions.append(
                {
                    "claim_id": ids.claim_id(envelope.id, i),
                    "citation_index": j,
                    "resolution": result.to_json(),
                }
            )
    if not binding.check_spec_ids:
        return _evaluation(binding, resolutions, structural_result)
    detail: dict[str, Any] = {}
    try:
        detail, eligible, hard = _v2_parts(binding, resolutions, structural_result)
        external = (
            adapters.source_coverage(binding=binding, eligible=eligible, resolver=resolver)
            if eligible and not hard
            else adapters.coverage_record(binding=binding, eligible=(), verdict=None)
        )
        return _evaluation_v2(binding, resolutions, structural_result, external)
    except Exception as error:
        # A required external stage cannot be replaced by an uncertainty or a PASS.
        return LayerResult(
            "rule_check",
            ERROR,
            "document check unavailable",
            {
                **detail,
                "assessment_error": f"{type(error).__name__}: {error}",
                "external_check": {"phase": "external", "execution": ERROR, "verdict": None},
            },
        )


def validated_assessments(
    layer: LayerResult | Mapping[str, Any], *, binding: AssessmentBindingV1
) -> tuple[CriterionAssessmentV1, ...]:
    recorded = _layer(layer)
    allowed = {PASS, "NEEDS_HUMAN"} if binding.check_spec_ids else {PASS}
    if recorded.layer != "rule_check" or recorded.status not in allowed:
        raise ContractError("only a recorded rule_check PASS/NEEDS_HUMAN can supply assessments")
    detail = recorded.detail
    if (
        detail.get("assessment_schema") != 1
        or detail.get("assessment_binding") != binding.to_json()
    ):
        raise ContractError("assessment frozen binding mismatch or missing")
    raw = detail.get("criterion_assessments")
    resolutions = detail.get("evidence_resolutions")
    if not isinstance(raw, (list, tuple)) or not isinstance(resolutions, (list, tuple)):
        raise ContractError("assessment detail catalogue is missing")
    parsed = tuple(CriterionAssessmentV1.from_json(item) for item in raw)
    structural = _layer(detail.get("structural_result"))
    if structural.status != PASS:
        raise ContractError("assessment cannot override a structural failure")
    if binding.check_spec_ids:
        external = detail.get("external_check")
        if not isinstance(external, Mapping):
            raise ContractError("missing external phase record")
        expected = _evaluation_v2(binding, resolutions, structural, external)
    else:
        expected = _evaluation(binding, resolutions, structural)
    # The recorder adds these two transport fields after the producer returns.
    compared = {
        key: value
        for key, value in recorded.detail.items()
        if key not in {"summary", "verifier_version"}
    }
    expected_detail = {
        key: value
        for key, value in expected.detail.items()
        if key not in {"summary", "verifier_version"}
    }
    if expected.status != recorded.status or compared != expected_detail:
        raise ContractError("assessment receipt, scope, references or criterion catalogue mismatch")
    return parsed


def inconclusive_retryable(
    layer: LayerResult | Mapping[str, Any], *, binding: AssessmentBindingV1
) -> bool:
    """Only a genuine pure missing-limitations failure; contextual conflicts run separately."""
    if not binding.check_spec_ids:
        return False
    try:
        recorded = _layer(layer)
        detail = recorded.detail
        if recorded.layer != "rule_check" or recorded.status != FAIL:
            return False
        if detail.get("assessment_binding") != binding.to_json():
            return False
        structural = _layer(detail.get("structural_result"))
        if structural.status != PASS or structural.detail.get("problems"):
            return False
        resolutions, external = detail.get("evidence_resolutions"), detail.get("external_check")
        if not isinstance(resolutions, (list, tuple)) or not isinstance(external, Mapping):
            return False
        expected = _evaluation_v2(binding, resolutions, structural, external)
        compared = {k: v for k, v in detail.items() if k not in {"summary", "verifier_version"}}
        expected_detail = {
            k: v for k, v in expected.detail.items() if k not in {"summary", "verifier_version"}
        }
        return (
            expected.status == FAIL
            and compared == expected_detail
            and expected.detail["hard_failures"] == ["missing_limitations"]
            and bool(expected.detail["limitations_check"]["required"])
        )
    except ContractError:
        return False


def doc_rule_reusable(
    layer: LayerResult | Mapping[str, Any], *, binding: AssessmentBindingV1
) -> bool:
    try:
        validated_assessments(layer, binding=binding)
    except ContractError:
        return False
    return True


__all__ = (
    "AssessmentBindingV1",
    "assessment_binding_for",
    "accepted_assessments_for",
    "citation_integrity",
    "criterion_id",
    "doc_rule_reusable",
    "inconclusive_retryable",
    "mission_contract_revision",
    "mission_criterion_catalog",
    "normalise_literal",
    "task_contract_revision",
    "validated_assessments",
)
