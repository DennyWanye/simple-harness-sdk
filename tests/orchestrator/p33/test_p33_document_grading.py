"""C01/C02/C06/C08: source attribution is the only document VERIFIED route.

Pure grading controls use explicit verifier records; the independent runtime and
commit tests exercise production creation and validation of those records.
"""

from dataclasses import replace

import pytest

from agent_orchestrator.contracts import ClaimProposal, ClaimStatus, ContractError, SourceCitation
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_PROFILE
from agent_orchestrator.memory.claims import grade_claim


def citation(quote="方案 A 不支持离线。", version="a", path="sources/a.md"):
    return SourceCitation(path, version * 64, 1, 1, quote)


def assessment(proposal, *, verdict="PASS", adapter="citation_integrity"):
    from agent_orchestrator.contracts import CriterionAssessmentV1

    refs = [
        {
            "schema": 1,
            "ref": c.to_json(),
            "kind": "markdown",
            "target": c.path,
            "status": "resolved",
            "source_version": c.version,
            "locator": {"start_line": 1, "end_line": 1},
            "display_block": {
                "start_line": 1,
                "end_line": 1,
                "headings": [],
                "preview": c.quote,
                "truncated": False,
            },
            "tenant_id": "tenant",
            "mission_id": "mission",
            "source_trust": "untrusted_external",
        }
        for c in proposal.citations
    ]
    body = {
        "schema": 1,
        "criterion_id": "criterion-" + "c" * 64,
        "task_contract_revision": "d" * 64,
        "claim_id": "claim-1",
        "claim_revision": 1,
        "output_ref": "result:r1",
        "output_hash": "e" * 64,
        "evidence_refs": refs,
        "source_versions": {c.path: c.version for c in proposal.citations},
        "verifier_adapter_id": adapter,
        "version": "1",
        "checked_scope": {
            "kind": "source_citation",
            "binding": "source_path",
            "criterion": "cite:sources/a.md",
        },
        "verdict": verdict,
        "provenance": {
            "schema": 1,
            "producer": "citation_integrity@v1",
            "binding_hash": "f" * 64,
            "tenant_id": "tenant",
            "mission_id": "mission",
            "task_id": "task",
            "attempt_id": "attempt",
            "claim_hash": sha256_hex(proposal.to_json()),
        },
    }
    body["receipt_id"] = "assessment-" + sha256_hex(body)
    return CriterionAssessmentV1.from_json(body)


def grade(proposal, assessments, **kwargs):
    return grade_claim(
        "claim-1",
        proposal.evidence,
        verifier_results=kwargs.pop("verifier_results", []),
        artifact_paths=["report.md"],
        untrusted_prefixes=["sources/"],
        domain=DOC_PROFILE,
        proposal=proposal,
        assessments=assessments,
        **kwargs,
    )


def test_attribution_content_key_stance_are_system_owned():
    c = citation()
    p = ClaimProposal(
        c.quote, 0.01, type="statement", key="world.offline", stance="refutes", citations=(c,)
    )
    result = grade(p, [assessment(p)])
    assert result.status is ClaimStatus.VERIFIED
    a = result.basis["attribution"]
    assert a["key"] == "attribution:" + "a" * 64 + ":1-1"
    assert a["content"] == "《sources/a.md》@aaaaaaaa #L1-L1 记载：「方案 A 不支持离线。」"
    assert a["stance"] == "affirms"
    assert result.basis["key_downgraded"] is True
    assert result.evidence_trust == ("untrusted_external",)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
@pytest.mark.parametrize("kind", ["statement", "attribution", "verified", "test"])
@pytest.mark.parametrize("critic", ["PASS", "FAIL"])
def test_nonliteral_claim_never_verified(confidence, kind, critic):
    p = ClaimProposal(
        "方案 A 支持离线。",
        confidence,
        type=kind,
        evidence=("pytest:any",),
        citations=(citation(),),
    )
    result = grade(
        p, [assessment(p)], verifier_results=[{"layer": "critic_review", "status": critic}]
    )
    assert result.status is ClaimStatus.SUPPORTED
    assert result.basis["scope_limited_to_source"] is True
    assert "attribution" not in result.basis
    if kind == "attribution":
        assert result.basis["type_downgraded"] is True


def test_missing_or_foreign_assessment_cannot_launder_a_quote():
    c = citation()
    p = ClaimProposal(c.quote, 1.0, citations=(c,))
    assert grade(p, []).status is ClaimStatus.UNDER_REVIEW
    assert grade(p, [assessment(p, adapter="code_test")]).status is ClaimStatus.UNDER_REVIEW
    other = replace(p, content="另一个主张。")
    assert grade(p, [assessment(other)]).status is ClaimStatus.UNDER_REVIEW


def test_primary_is_stable_under_citation_order_and_same_line_sentences_are_distinct():
    first, second = citation(), citation(version="b", path="sources/b.md")
    p = ClaimProposal(first.quote, 0.5, citations=(second, first))
    q = replace(p, citations=(first, second))
    a = grade(p, [assessment(p)]).basis["attribution"]
    b = grade(q, [assessment(q)]).basis["attribution"]
    assert a == b
    other = replace(p, content="方案 B 支持离线。", citations=(citation("方案 B 支持离线。"),))
    d = grade(other, [assessment(other)]).basis["attribution"]
    assert a["key"] == d["key"]
    assert a["identity"] != d["identity"]


@pytest.mark.parametrize(
    "evidence,expected",
    [([], "UNDER_REVIEW"), (["file:report.md"], "SUPPORTED"), (["pytest:tests/a.py"], "VERIFIED")],
)
def test_code_legacy_inputs_keep_the_same_results(evidence, expected):
    kwargs = dict(
        verifier_results=[
            {"layer": "code_test", "detail": {"runs": [{"target": "tests/a.py", "passed": True}]}}
        ],
        artifact_paths=["report.md"],
        untrusted_prefixes=[],
    )
    default = grade_claim("c", evidence, **kwargs)
    explicit = grade_claim(
        "c", evidence, domain=CODE_PROFILE, proposal=ClaimProposal("x", 1), assessments=(), **kwargs
    )
    assert default.to_json() == explicit.to_json()
    assert str(default.status) == expected


@pytest.mark.parametrize("kind", ["statement", "attribution"])
def test_system_wrapper_allowance_does_not_expand_model_input(kind):
    text = "甲" * 20_001
    with pytest.raises(ContractError, match="exceeds 20000"):
        ClaimProposal(text, 1, type=kind)
    with pytest.raises(ContractError, match="exceeds 20000"):
        SourceCitation("sources/a.md", "a" * 64, 1, 1, text)
