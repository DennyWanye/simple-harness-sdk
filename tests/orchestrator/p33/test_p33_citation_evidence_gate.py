"""C03: typed document citations replace string evidence, but code stays legacy."""

from dataclasses import replace

import pytest

from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.contracts import Budget, ClaimProposal, ResultEnvelope, SourceCitation, Task
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_PROFILE
from agent_orchestrator.verification.deterministic_checks import rule_check


@pytest.mark.parametrize(
    "domain,has_citation,expected",
    [(DOC_PROFILE, True, "PASS"), (DOC_PROFILE, False, "FAIL"), (CODE_PROFILE, True, "FAIL")],
)
def test_typed_citation_satisfies_doc_evidence_presence_only(
    tmp_path, domain, has_citation, expected
):
    workspace = WorkspaceManager(tmp_path / "workspaces").create(
        "attempt", seed={"report.md": "record"}
    )
    artifacts = workspace.snapshot(mission_id="mission", task_id="task", produced_by="worker")
    task = Task(
        id="task",
        mission_id="mission",
        parent_task_ids=(),
        dependency_ids=(),
        allowed_tools=(),
        priority=1.0,
        status="ACTIVE",
        version=1,
        goal="write record",
        rationale="record",
        success_criteria=("file:report.md",),
        verification_policy=("format_check", "rule_check"),
        budget=Budget(max_tokens=1000),
    )
    proposal = ClaimProposal(
        "来源原句。",
        0.5,
        citations=(SourceCitation("sources/a.md", "a" * 64, 1, 1, "来源原句。"),)
        if has_citation
        else (),
    )
    envelope = ResultEnvelope(
        id="result",
        task_id=task.id,
        mission_id=task.mission_id,
        attempt_id="attempt",
        outcome="candidate",
        summary="record",
        claims=(proposal,),
        evidence=(),
        artifacts=("report.md",),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    result = rule_check(
        envelope, task, artifacts=artifacts, verification_copy=workspace, domain=domain
    )
    assert result.status == expected
    if expected == "PASS":
        # Presence is not resolution; the separate producer must still reject a bad quote.
        assert "criterion_assessments" not in result.detail
        mixed = replace(envelope, claims=(proposal, ClaimProposal("无来源结论", 1)))
        assert (
            rule_check(
                mixed, task, artifacts=artifacts, verification_copy=workspace, domain=domain
            ).status
            == "FAIL"
        )
