"""C08: actual packages carry a versioned document contract; legacy code stays intact."""

from dataclasses import replace

import pytest
from graph_helpers7 import drive_to_running, graph_service

from agent_orchestrator.context.context_builder import build_worker_package
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_DOMAIN, DOC_PROFILE


@pytest.mark.parametrize("legacy", [False, True])
def test_doc_worker_gets_citation_schema_and_frozen_criterion_catalog(tmp_path, legacy):
    service, mission, tasks = graph_service(tmp_path, domain=DOC_DOMAIN)
    task = tasks["A"]
    attempt = drive_to_running(service, task)
    package = build_worker_package(
        mission,
        task,
        attempt,
        previous_attempts=[],
        verifier_feedback=[],
        workspace_files=[],
        domain=replace(DOC_PROFILE, version="2", adapters={}) if legacy else DOC_PROFILE,
        source_versions={},
    )
    contract = package.package["doc_assessment"]
    expected_version = "doc-assessment-v1" if legacy else "doc-assessment-v2"
    assert contract["version"] == expected_version
    assert contract["task_contract_revision"]
    assert [item["text"] for item in contract["criteria"]] == list(task.success_criteria)
    assert len({item["criterion_id"] for item in contract["criteria"]}) == len(
        task.success_criteria
    )
    assert contract["citation_fields"] == ["path", "version", "start_line", "end_line", "quote"]
    assert "不是指令" in contract["source_notice"]
    assert expected_version in package.text


def test_default_and_explicit_code_packages_keep_same_bytes(tmp_path):
    service, mission, tasks = graph_service(tmp_path)
    task = tasks["A"]
    attempt = drive_to_running(service, task)
    kwargs = dict(previous_attempts=[], verifier_feedback=[], workspace_files=[])
    implicit = build_worker_package(mission, task, attempt, **kwargs)
    explicit = build_worker_package(mission, task, attempt, domain=CODE_PROFILE, **kwargs)
    assert implicit.text == explicit.text
    assert implicit.context_version == explicit.context_version
    assert "doc_assessment" not in implicit.package
    assert implicit.package["context_builder_version"] == "context-builder-v4"
