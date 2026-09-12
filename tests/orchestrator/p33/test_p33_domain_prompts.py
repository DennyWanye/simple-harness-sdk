# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice A D1/A07: frozen domain words reach the actual dispatch intent.

Oracle: document agents must not be instructed to manufacture pytest evidence;
code defaults must keep their exact prompts and context; changing the deployment's
domain registry cannot change a Mission that has already frozen its domain.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fixtures_provider import RoleScriptedProvider
from graph_helpers7 import drive_to_running, graph_service, node, spec

from agent_orchestrator.context.context_builder import build_planner_package, build_worker_package
from agent_orchestrator.governance import domains
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain


def test_legacy_domain_snapshot_missing_fields_has_fixed_compatibility():
    legacy = domains.DOC_PROFILE.to_json()
    legacy["version"] = "1"
    legacy.pop("role_templates")
    legacy.pop("context_wording")
    interpreted = domains.DomainProfileV1.from_json(legacy)
    assert interpreted.role_templates == domains.DOC_ROLE_TEMPLATES_V1
    assert interpreted.context_wording == domains.DOC_CONTEXT_WORDING_V1
    legacy["role_templates"] = {}
    assert domains.DomainProfileV1.from_json(legacy).role_templates == {}
    old_code = domains.CODE_PROFILE.to_json()
    old_code.pop("role_templates")
    old_code.pop("context_wording")
    assert domains.DomainProfileV1.from_json(old_code) == domains.CODE_PROFILE


def test_frozen_domain_override_is_strict_and_precedes_policy():
    from agent_orchestrator.contracts import ContractError

    worker = ROLES["worker"]
    chosen = template_for_domain(worker, domains.DOC_PROFILE, {"worker": worker.prompt_version})
    assert chosen.prompt_version == domains.DOC_PROFILE.role_templates["worker"]
    bad = replace(domains.DOC_PROFILE, role_templates={"worker": "missing-version"})
    with pytest.raises(ContractError, match="unavailable domain prompt"):
        template_for_domain(worker, bad, {})


@pytest.mark.parametrize(
    "damage", ["schema", "identity", "missing", "role_templates", "context_wording"]
)
def test_invalid_bound_snapshot_cannot_fall_back_to_registry(tmp_path, monkeypatch, damage):
    from agent_orchestrator.orchestrator.commit_service import CommitRejected

    service, mission, _ = graph_service(tmp_path, domain=domains.DOC_DOMAIN)
    binding = service.store.get_mission_domain(mission.id)
    if damage == "schema":
        binding["json"]["schema"] = 999
    elif damage == "identity":
        binding["json"]["id"] = domains.CODE_DOMAIN
    elif damage == "missing":
        binding["json"].pop("default_policy")
    else:
        binding["json"].pop(damage)
    monkeypatch.setattr(service.store, "get_mission_domain", lambda _: binding)
    with pytest.raises(CommitRejected, match="invalid frozen domain"):
        service.domain_for(mission.id)


def test_planner_domain_exposes_only_deployed_domain_layers(tmp_path):
    _, mission, _ = graph_service(tmp_path, domain=domains.DOC_DOMAIN)
    package = build_planner_package(
        mission,
        workspace_files=[],
        attempt_ordinal=1,
        deployed_layers=frozenset({"format_check", "rule_check", "code_test"}),
        domain=domains.DOC_PROFILE,
    ).package
    assert package["deployed_verification_layers"] == ["format_check", "rule_check"]
    assert package["domain"]["verification_floor"] == ["format_check", "rule_check"]
    assert "pytest" not in package["domain"]["criterion_kinds"]


@pytest.mark.parametrize("role", list(ROLES))
def test_doc_prompt_uses_document_rules_and_preserves_role_identity(tmp_path, role):
    async def case():
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), RoleScriptedProvider({})
        ) as orch:
            mission = await orch.submit_mission(spec(domain=domains.DOC_DOMAIN))
            selected = orch._template(ROLES[role], mission.id)
            assert selected.name == role
            assert selected.instructions.startswith(f"[role:{role}]")
            assert "pytest:" not in selected.instructions
            assert "run_tests" not in selected.tool_names
            assert "来源" in selected.instructions
            assert selected.prompt_version != ROLES[role].prompt_version

    asyncio.run(case())


def test_code_prompt_defaults_are_unchanged(tmp_path):
    async def case():
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), RoleScriptedProvider({})
        ) as orch:
            mission = await orch.submit_mission(spec())
            for template in ROLES.values():
                assert orch._template(template, mission.id) == template

    asyncio.run(case())


def test_published_document_prompts_do_not_follow_future_code_defaults(monkeypatch):
    from agent_orchestrator.runtime import role_templates
    from agent_orchestrator.runtime.domain_templates import register_document_templates

    before = {
        name: template_for_domain(template, domains.DOC_PROFILE, {})
        for name, template in ROLES.items()
    }
    monkeypatch.setattr(
        role_templates, "WORKER",
        replace(role_templates.WORKER, prompt_version="worker-future", instructions="future"),
    )
    register_document_templates()
    assert {
        name: template_for_domain(template, domains.DOC_PROFILE, {})
        for name, template in ROLES.items()
    } == before


def test_domain_snapshot_survives_registry_change_and_database_reopen(tmp_path, monkeypatch):
    service, mission, _ = graph_service(tmp_path, domain=domains.DOC_DOMAIN)
    frozen = service.domain_for(mission.id).to_json()
    changed = replace(domains.DOC_PROFILE, criterion_kinds=("pytest",), version="future")
    monkeypatch.setattr(
        domains, "DOMAINS", {domains.CODE_DOMAIN: domains.CODE_PROFILE, domains.DOC_DOMAIN: changed}
    )
    assert service.domain_for(mission.id).to_json() == frozen
    from agent_orchestrator.orchestrator.commit_service import CommitService
    from agent_orchestrator.storage.store import Store

    reopened = Store.open(tmp_path / "orchestrator.db")
    try:
        assert CommitService(reopened).domain_for(mission.id).to_json() == frozen
    finally:
        reopened.close()


@pytest.mark.parametrize("role", ["worker", "arbiter", "synthesizer"])
def test_context_visibility_obeys_document_domain(tmp_path, role):
    service, mission, tasks = graph_service(tmp_path, domain=domains.DOC_DOMAIN)
    task = tasks["A"]
    attempt = drive_to_running(service, task)
    kwargs = dict(previous_attempts=[], verifier_feedback=[], workspace_files=[], role=role)
    legacy = build_worker_package(mission, task, attempt, **kwargs)
    assert (
        build_worker_package(mission, task, attempt, domain=domains.CODE_PROFILE, **kwargs)
        == legacy
    )
    document = build_worker_package(
        mission, task, attempt, domain=service.domain_for(mission.id), **kwargs
    )
    assert "pytest" not in document.package["visibility"]
    assert "来源原文" in document.package["visibility"]
    assert document.context_version != legacy.context_version


def test_document_runtime_freezes_its_actual_prompt_and_context(tmp_path):
    async def case():
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), RoleScriptedProvider({})
        ) as orch:
            mission = await orch.submit_mission(spec(domain=domains.DOC_DOMAIN))
            planning = orch.commit.begin_planning(mission.id)
            planner = await orch._create_planner_intent(mission.id, ordinal=1)
            assert "pytest:" not in planner.config["agent_config"]["instructions"]
            assert domains.DOC_DOMAIN in str(planner.config["message"])
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node("A")]}),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            mission = orch.store.get_mission(mission.id)
            assert await orch._next_attempt(mission, tasks[0], [])
            [attempt] = orch.store.list_attempts(tasks[0].id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert "pytest:" not in intent.config["agent_config"]["instructions"]
            assert "run_tests" not in intent.config["agent_config"]["tool_names"]
            assert "来源原文" in str(intent.config["message"])
            assert intent.config["prompt_version"] == attempt.prompt_version
            assert domains.DOC_DOMAIN in attempt.prompt_version

    asyncio.run(case())
