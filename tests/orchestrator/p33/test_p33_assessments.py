# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""C oracle, written before implementation. No model-generated assessment receipts.

The repository double isolates durable binding reads; the CAS, resolver, producer,
contract and router are real. SQLite transactions/acceptance are tested separately.
"""

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import (
    ClaimProposal,
    ContractError,
    ResultEnvelope,
    SourceCitation,
)
from agent_orchestrator.contracts.assessments import CriterionAssessmentV1
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_PROFILE
from agent_orchestrator.verification.assessments import (
    assessment_binding_for,
    citation_integrity,
    criterion_id,
    doc_rule_reusable,
    task_contract_revision,
    validated_assessments,
)
from agent_orchestrator.verification.deterministic_checks import LayerResult
from agent_orchestrator.verification.evidence_resolver import EvidenceResolver
from agent_orchestrator.verification.verifier_router import VerifierRouter


class Repository:
    def get_mission(self, mid):
        return self.mission if mid == self.mission.id else None

    def get_intent_for_subject(self, aid):
        return self.intent if aid == "attempt" else None

    def get_claim(self, cid):
        return self.claims.get(cid)

    def get_source(self, mid, path, version_hash=None):
        assert version_hash is not None, "never consult a mutable source head"
        self.lookups.append((mid, path, version_hash))
        return self.sources.get((mid, path, version_hash))


@pytest.fixture
def scene(tmp_path):
    repo = Repository()
    repo.mission = SimpleNamespace(id="mission", tenant_id="tenant")
    repo.sources, repo.lookups, repo.claims = {}, [], {}
    cas = ArtifactStore(tmp_path / "cas")
    task = SimpleNamespace(
        id="task",
        mission_id="mission",
        kind="work",
        goal="核查来源",
        rationale="保存依据",
        success_criteria=("cite:sources/a.md",),
        verification_policy=("rule_check",),
        outputs=("report.md",),
        version=17,
        context={},
    )
    attempt = SimpleNamespace(id="attempt", task_id="task", mission_id="mission")
    artifact = SimpleNamespace(
        id="artifact",
        path="report.md",
        content_hash="f" * 64,
        mission_id="mission",
        task_id="task",
        attempt_id="attempt",
    )

    def register(text="方案有效。\n", path="sources/a.md"):
        version = cas.put_bytes(text.encode())
        repo.sources[("mission", path, version)] = {
            "tenant_id": "tenant",
            "mission_id": "mission",
            "path": path,
            "version_hash": version,
            "kind": "markdown",
            "trust": "untrusted_external",
        }
        return SourceCitation(path, version, 1, len(text.splitlines()), "方案有效。")

    def bind(proposals=None, criteria=None):
        if criteria is not None:
            task.success_criteria = tuple(criteria)
        proposals = (
            proposals
            if proposals is not None
            else [ClaimProposal("方案有效。", 0.8, citations=(register(),))]
        )
        envelope = ResultEnvelope.from_json(
            {
                "id": "result",
                "mission_id": "mission",
                "task_id": "task",
                "attempt_id": "attempt",
                "outcome": "candidate",
                "summary": "报告",
                "artifacts": ["report.md"],
                "claims": [p.to_json() for p in proposals],
            }
        )
        contract = {
            name: getattr(task, name)
            for name in (
                "kind",
                "goal",
                "rationale",
                "success_criteria",
                "verification_policy",
                "outputs",
            )
        }
        contract["task_id"] = task.id
        repo.intent = SimpleNamespace(
            kind="attempt",
            subject_id="attempt",
            mission_id="mission",
            config={
                "task_contract": contract,
                "source_roots": ["sources/"],
                "source_versions": {p: v for m, p, v in repo.sources},
            },
        )
        repo.claims = {
            f"result:claim-{i}": SimpleNamespace(
                id=f"result:claim-{i}",
                version=i + 2,
                mission_id="mission",
                result_id="result",
                source_task="task",
                source_attempt="attempt",
                content=p.content,
            )
            for i, p in enumerate(proposals, 1)
        }
        return envelope, assessment_binding_for(
            repo, task=task, attempt=attempt, envelope=envelope, artifacts=[artifact]
        )

    return SimpleNamespace(
        repo=repo,
        cas=cas,
        task=task,
        attempt=attempt,
        artifact=artifact,
        register=register,
        bind=bind,
        resolver=EvidenceResolver(repo, cas),
    )


def produce(s, envelope, binding, status="PASS"):
    return citation_integrity(
        binding=binding,
        envelope=envelope,
        resolver=s.resolver,
        structural_result=LayerResult("rule_check", status, "existing checks", {"original": True}),
    )


def test_source_scope_has_actual_claim_identity_and_complete_receipt(scene):
    envelope, binding = scene.bind()
    layer = produce(scene, envelope, binding)
    assert layer.status == "PASS"
    [assessment] = validated_assessments(layer, binding=binding)
    raw = assessment.to_json()
    assert raw["claim_id"] == "result:claim-1" and raw["claim_revision"] == 3
    assert raw["output_ref"] == "result" and raw["output_hash"] == binding.output_hash
    assert raw["verifier_adapter_id"] == "citation_integrity" and raw["version"] == "1"
    assert raw["checked_scope"] == {
        "kind": "source_citation",
        "binding": "source_path",
        "criterion": "cite:sources/a.md",
    }
    assert raw["evidence_refs"][0]["locator"] == {"start_line": 1, "end_line": 1}
    assert raw["evidence_refs"][0]["source_trust"] == "untrusted_external"
    assert raw["provenance"]["binding_hash"] == binding.binding_hash
    assert all(
        value is not None and value != "" and value != [] and value != {} for value in raw.values()
    )
    assert CriterionAssessmentV1.from_json(raw).to_json() == raw
    with pytest.raises(TypeError):
        assessment.provenance["producer"] = "worker"


def test_free_binding_is_nfc_whitespace_equality_not_contains_or_nfkc(scene):
    citation = scene.register("Cafe\u0301  有效。\n")
    citation = replace(citation, quote="Café 有效。")
    proposal = ClaimProposal("Café\t有效。", 0.5, citations=(citation,))
    env, bound = scene.bind([proposal], ["Cafe\u0301 有效。"])
    layer = produce(scene, env, bound)
    assert layer.status == "PASS"
    assert validated_assessments(layer, binding=bound)[0].checked_scope["binding"] == "literal"
    for criterion in ("有效。", "Ｃafé 有效。", "请证明 Café 有效。"):
        env, bound = scene.bind([proposal], [criterion])
        assert produce(scene, env, bound).status == "FAIL"


@pytest.mark.parametrize(
    "criteria", [("file:report.md",), ("action:publish",), ("arbitration:key",)]
)
def test_structural_scope_does_not_invent_a_claim_assessment(scene, criteria):
    env, bound = scene.bind(criteria=criteria)
    layer = produce(scene, env, bound)
    assert layer.status == "PASS"
    assert validated_assessments(layer, binding=bound) == ()
    [verdict] = layer.detail["criterion_verdicts"]
    assert verdict["claim_ids"] == []
    assert (
        verdict["scope"]
        == {
            "file": "file_exists",
            "action": "action_candidate_schema_deployment_charter",
            "arbitration": "arbitration_claim_structure",
        }[criteria[0].split(":")[0]]
    )


def test_unlisted_collected_side_artifact_is_allowed_but_all_hashes_are_bound(scene):
    env, original = scene.bind()
    side = SimpleNamespace(
        id="side",
        path="notes.md",
        content_hash="a" * 64,
        mission_id="mission",
        task_id="task",
        attempt_id="attempt",
    )

    def binding(artifacts):
        return assessment_binding_for(
            scene.repo, task=scene.task, attempt=scene.attempt, envelope=env, artifacts=artifacts
        )

    added = binding([scene.artifact, side])
    assert added.output_hash != original.output_hash
    assert binding([side, scene.artifact]).output_hash == added.output_hash
    layer = produce(scene, env, added)
    assert validated_assessments(layer, binding=added)
    side.content_hash = "b" * 64
    assert not doc_rule_reusable(layer, binding=binding([scene.artifact, side]))
    with pytest.raises(ContractError):
        binding([side])  # a listed output must still exist in the collected set


@pytest.mark.parametrize("case", ["missing", "unbound", "wrong_source", "one_bad", "unbound_bad"])
def test_file_pass_never_masks_missing_binding_or_failed_submitted_citation(scene, case):
    good = scene.register()
    bad = replace(good, quote="不在来源中的句子。")
    proposals = [ClaimProposal("方案有效。", 0.8, citations=() if case == "missing" else (good,))]
    criteria = ["file:report.md", "cite:sources/a.md"]
    if case == "unbound":
        criteria[1] = "比较多个方案。"
    if case == "wrong_source":
        criteria[1] = "cite:sources/b.md"
    if case == "one_bad":
        proposals[0] = replace(proposals[0], citations=(good, bad))
    if case == "unbound_bad":
        proposals.append(ClaimProposal("另一条无关联主张。", 0.1, citations=(bad,)))
    env, bound = scene.bind(proposals, criteria)
    layer = produce(scene, env, bound)
    assert layer.status == "FAIL"
    assert len(layer.detail["criterion_verdicts"]) == 2
    if case in {"one_bad", "unbound_bad"}:
        assert any(
            item["resolution"]["status"] == "quote_mismatch"
            for item in layer.detail["evidence_resolutions"]
        )
    assert not doc_rule_reusable(layer, binding=bound)
    with pytest.raises(ContractError):
        validated_assessments(layer, binding=bound)


@pytest.mark.parametrize("status", ["FAIL", "ERROR", "NOT_REQUIRED"])
def test_successful_citation_never_upgrades_structural_failure(scene, status):
    env, bound = scene.bind()
    layer = produce(scene, env, bound, status=status)
    assert layer.status != "PASS"
    assert not doc_rule_reusable(layer, binding=bound)


@pytest.mark.parametrize(
    "damage",
    [
        "directory",
        "duplicate",
        "receipt",
        "claim_revision",
        "output_hash",
        "scope",
        "source_subset",
        "tenant",
        "binding_roots",
        "missing_resolution",
        "unsubmitted_ref",
    ],
)
def test_reuse_and_accept_apply_the_same_complete_validation(scene, damage):
    env, bound = scene.bind()
    layer = produce(scene, env, bound)
    raw = copy.deepcopy(layer.to_json())
    detail = raw["detail"]
    assessment = detail["criterion_assessments"][0]
    if damage == "directory":
        detail["criterion_verdicts"].clear()
    elif damage == "duplicate":
        detail["criterion_assessments"].append(copy.deepcopy(assessment))
    elif damage == "binding_roots":
        detail["assessment_binding"]["source_roots"] = ["other/"]
    elif damage == "missing_resolution":
        detail["evidence_resolutions"].clear()
    elif damage == "receipt":
        assessment["receipt_id"] = "assessment-" + "0" * 64
    else:
        if damage == "claim_revision":
            assessment["claim_revision"] += 1
        elif damage == "output_hash":
            assessment["output_hash"] = "0" * 64
        elif damage == "scope":
            assessment["checked_scope"]["kind"] = "world_truth"
        elif damage == "source_subset":
            assessment["source_versions"]["sources/unused.md"] = "a" * 64
        elif damage == "tenant":
            assessment["provenance"]["tenant_id"] = "other"
        elif damage == "unsubmitted_ref":
            assessment["evidence_refs"][0]["ref"]["quote"] = "伪造引文。"
        # A self-consistent checksum is not proof of the binding or evaluation.
        assessment["receipt_id"] = "assessment-" + sha256_hex(
            {k: v for k, v in assessment.items() if k != "receipt_id"}
        )
    with pytest.raises(ContractError):
        validated_assessments(raw, binding=bound)
    assert not doc_rule_reusable(raw, binding=bound)


@pytest.mark.parametrize("damage", ["unknown", "bool_revision", "null_claim", "empty_refs"])
def test_assessment_contract_rejects_invalid_shapes(scene, damage):
    env, bound = scene.bind()
    raw = produce(scene, env, bound).detail["criterion_assessments"][0]
    raw = copy.deepcopy(raw)
    if damage == "unknown":
        raw["worker_grade"] = "VERIFIED"
    elif damage == "bool_revision":
        raw["claim_revision"] = True
    elif damage == "null_claim":
        raw["claim_id"] = None
    else:
        raw["evidence_refs"] = []
    with pytest.raises(ContractError):
        CriterionAssessmentV1.from_json(raw)


def test_frozen_contract_and_claim_revision_not_mutable_task_state(scene):
    env, bound = scene.bind(criteria=["方案有效。", "方案有效。"])
    assert len({c["id"] for c in bound.criteria}) == 2
    contract = scene.repo.intent.config["task_contract"]
    assert (
        task_contract_revision({**contract, "status": "COMPLETED", "version": 99})
        == bound.task_contract_revision
    )
    assert criterion_id(bound.task_contract_revision, 1, "方案有效。") != criterion_id(
        bound.task_contract_revision, 2, "方案有效。"
    )
    scene.task.version += 20
    rebound = assessment_binding_for(
        scene.repo, task=scene.task, attempt=scene.attempt, envelope=env, artifacts=[scene.artifact]
    )
    assert rebound.to_json() == bound.to_json()
    scene.task.goal = "另一份合同"
    with pytest.raises(ContractError):
        assessment_binding_for(
            scene.repo,
            task=scene.task,
            attempt=scene.attempt,
            envelope=env,
            artifacts=[scene.artifact],
        )


@pytest.mark.parametrize("message_kind", ["json", "sections"])
def test_pending_legacy_contract_is_read_only_from_durable_message(scene, message_kind):
    env, bound = scene.bind()
    config = scene.repo.intent.config
    contract = config.pop("task_contract")
    text = (
        json.dumps({"task_contract": contract})
        if message_kind == "json"
        else "## task_contract\n" + json.dumps(contract) + "\n\n## feedback\n[]"
    )
    config["message"] = {"content": text}
    before = copy.deepcopy(config)
    rebound = assessment_binding_for(
        scene.repo, task=scene.task, attempt=scene.attempt, envelope=env, artifacts=[scene.artifact]
    )
    assert rebound.to_json() == bound.to_json()
    assert config == before
    del config["message"]
    with pytest.raises(ContractError):
        assessment_binding_for(
            scene.repo,
            task=scene.task,
            attempt=scene.attempt,
            envelope=env,
            artifacts=[scene.artifact],
        )


def test_missing_frozen_sources_never_acquires_current_registry(scene):
    env, _ = scene.bind()
    del scene.repo.intent.config["source_versions"]
    bound = assessment_binding_for(
        scene.repo, task=scene.task, attempt=scene.attempt, envelope=env, artifacts=[scene.artifact]
    )
    assert dict(bound.source_versions) == {}
    assert produce(scene, env, bound).status == "FAIL"


def test_router_reruns_legacy_doc_rule_but_reuses_complete_receipt(scene, monkeypatch):
    import agent_orchestrator.verification.verifier_router as module

    env, bound = scene.bind()
    calls = []

    def structure(*args, **kwargs):
        calls.append("structure")
        return LayerResult("rule_check", "PASS", "structure", {})

    monkeypatch.setattr(module, "rule_check", structure)

    async def critic(_):
        raise AssertionError("not required")

    async def run(reuse, domain=DOC_PROFILE):
        return await VerifierRouter(domain=domain).verify(
            mission=scene.repo.mission,
            task=scene.task,
            envelope=env,
            artifacts=[],
            verification_copy=None,
            client_result_id=None,
            run_critic=critic,
            reuse=reuse,
            assessment_binding=bound,
            evidence_resolver=scene.resolver,
        )

    old = LayerResult("rule_check", "PASS", "old no assessments", {})
    first = asyncio.run(run({"rule_check": old}))
    assert first.passed and calls == ["structure"]
    rule = next(r for r in first.layers if r.layer == "rule_check")
    reads = list(scene.repo.lookups)
    assert asyncio.run(run({"rule_check": rule})).passed
    assert calls == ["structure"] and scene.repo.lookups == reads
    assert asyncio.run(run({"rule_check": old}, CODE_PROFILE)).passed
    assert calls == ["structure"]  # legacy keeps its existing reuse semantics


def test_router_per_call_domain_does_not_mutate_shared_default(scene, monkeypatch):
    import agent_orchestrator.verification.verifier_router as module

    env, bound = scene.bind()
    calls = []

    def structure(*args, **kwargs):
        calls.append(kwargs["domain"].id)
        return LayerResult("rule_check", "PASS", "structure", {})

    monkeypatch.setattr(module, "rule_check", structure)

    async def critic(_):
        raise AssertionError("not required")

    router = VerifierRouter(domain=CODE_PROFILE)

    async def run():
        args = dict(
            mission=scene.repo.mission,
            task=scene.task,
            envelope=env,
            artifacts=[],
            verification_copy=None,
            client_result_id=None,
            run_critic=critic,
        )
        doc = await router.verify(
            **args, domain=DOC_PROFILE, assessment_binding=bound, evidence_resolver=scene.resolver
        )
        code = await router.verify(**args)
        return doc, code

    doc, code = asyncio.run(run())
    assert calls == [DOC_PROFILE.id, CODE_PROFILE.id]
    assert "criterion_assessments" in next(r.detail for r in doc.layers if r.layer == "rule_check")
    assert "criterion_assessments" not in next(
        r.detail for r in code.layers if r.layer == "rule_check"
    )
