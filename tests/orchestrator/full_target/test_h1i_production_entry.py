# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-I production-entry regressions.

These tests deliberately enter through :class:`Orchestrator` and its real
``_create_planner_intent`` / ``_collect_plan_decision`` path.  The only injected
provider is the repository's deterministic scripted provider; it is used to keep
this regression suite offline and to leave the real model run to the H1-I gate.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_FULL_TARGET = Path(__file__).resolve().parent
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

import test_htn_end_to_end as e2e  # noqa: E402
import test_real_provider_hierarchical_smoke as real_smoke  # noqa: E402

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi  # noqa: E402
from agent_orchestrator.api.operation_completion import OperationCompletionApi  # noqa: E402
from agent_orchestrator.contracts import Budget  # noqa: E402
from agent_orchestrator.contracts.obligations import Obligation  # noqa: E402
from agent_orchestrator.contracts.planning_decisions import (  # noqa: E402
    PlanningDecisionEnvelopeV1,
    PlanningDecisionStatus,
)
from agent_orchestrator.governance.permissions import Principal  # noqa: E402
from agent_orchestrator.governance.policies import deployed_layers  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    CommitService,
    MissionSpec,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    HierarchicalDispatch,
)
from agent_orchestrator.orchestrator.plan_commits import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    PlanCommitRejected,
)
from agent_orchestrator.orchestrator.root_review import root_requirements  # noqa: E402
from agent_orchestrator.planning.decision_codec import serialize_planning_decision  # noqa: E402
from agent_orchestrator.planning.htn.observers.code import code_observers  # noqa: E402
from agent_orchestrator.planning.htn.world import build_planning_world  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.runtime.role_templates import PLANNER_HIERARCHICAL_V9, PLANNER_HIERARCHICAL_V10  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402

ROOT_TASK = e2e.ROOT_TASK
ROOT_DUTY = e2e.ROOT_DUTY
FUEL = e2e.FUEL


def _approve_root_content_only_spec(commit, mission, binding, *, command_id: str):
    """Publish and explicitly confirm the root's actual requirement contract.

    The fixture keeps Mission requirements separate from deterministic leaf-review
    policy: this is a human confirmation of the frozen root criteria, through the
    production API, never an inferred CONTENT_ONLY fallback or test-made coverage.
    """

    requirements = root_requirements(mission.id, binding, revision=1)
    HtnStore(commit.store).insert_requirements_revision(requirements)
    requirements_ref = {
        "kind": "requirements",
        "id": str(requirements.revision_id),
        "revision": int(requirements.revision),
        "content_hash": requirements.content_hash(),
    }
    OperationCompletionApi(
        commit,
        tenant_id=mission.tenant_id,
        principal=Principal("human-completion-fixture"),
    ).approve(
        {
            "mission_id": mission.id,
            "command_id": command_id,
            "expected_requirements_ref": requirements_ref,
            "proposal": {
                "schema_version": 1,
                "mission_id": mission.id,
                "requirements_ref": {
                    "id": str(requirements.revision_id),
                    "revision": int(requirements.revision),
                    "content_hash": requirements.content_hash(),
                },
                "mode": "CONTENT_ONLY",
                "content_criterion_ids": list(requirements.required_criterion_ids()),
                "effects": [],
            },
        }
    )
    return requirements


def test_historical_planning_package_label_keeps_original_integer_binding() -> None:
    assert Orchestrator._planning_decision_package_version(
        {"package_version": "planner-package-hierarchical-v5"}
    ) == 4
    assert Orchestrator._planning_decision_package_version(
        {"package_version": "planner-package-hierarchical-v6"}
    ) == 5


def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        evidence_root=tmp_path / "evidence",
        max_concurrency=1,
        max_concurrent_model_calls=1,
        max_planning_attempts=3,
        max_root_review_repairs=1,
        test_timeout_seconds=30,
    )


def _seed_new_protocol(
    loop: Orchestrator, tmp_path: Path, *, key: str
) -> tuple[Any, Any, Any, HierarchicalDispatch]:
    """Seed the real code-domain HTN world used by the production smoke."""

    repo = real_smoke._repo(tmp_path / "repo")

    mission, _ = loop.commit.create_mission(
        MissionSpec(
            goal=(
                "修复仓库里失败的测试 tests/test_kv.py::test_parse_kv_strips_whitespace，"
                "并说明改动；不要改测试本身。"
            ),
            success_criteria=("pytest:tests/test_kv.py", "改动有说明"),
            tenant_id="tenant-h1i",
            idempotency_key=key,
            allowed_tools=(
                "workspace_read_file",
                "workspace_write_file",
                "workspace_list",
                "run_tests",
            ),
            budget=Budget(max_tokens=200_000, max_attempts=8),
            orchestration_semantics_version=HIERARCHICAL_SEMANTICS,
            planning_protocol_version="planning-decision-v1",
        )
    )
    semantics = HtnStore(loop.store)
    env = build_planning_world(
        mission.id,
        domains=("code",),
        semantics=semantics,
        deployed_layers=deployed_layers(loop._config.deployment_policy),
        observers=code_observers(repo, allow_test_execution=True),
    )
    dispatch = loop.install_hierarchical(planning=env)
    binding = real_smoke._root_binding(env, str(repo))
    ObligationStore(loop.store).register(
        Obligation(
            obligation_id=ROOT_DUTY,
            mission_id=mission.id,
            requirement_refs=tuple(binding.goal_signature.coverage_criteria),
            goal_signature_id=real_smoke.GOAL_TYPE,
        ),
        recursion_fuel=FUEL,
    )
    loop.commit.admit_obligation_demand(
        mission.id,
        ROOT_DUTY,
        principal="mission-submitter",
        requester={"kind": "mission_root"},
        evidence={
            "mission_id": mission.id,
            "requirement_refs": list(binding.goal_signature.coverage_criteria),
        },
    )
    semantics.put_task_semantics(mission.id, binding)
    _approve_root_content_only_spec(
        loop.commit,
        mission,
        binding,
        command_id=f"approve-completion-{key}",
    )
    real_smoke._look(env, semantics, mission.id, str(repo))
    loop.commit.begin_planning(mission.id)
    return mission, env, binding, dispatch


async def _open_planner_round(
    loop: Orchestrator,
    mission: Any,
    dispatch: HierarchicalDispatch,
    *,
    ordinal: int,
):
    intent = await loop._create_planner_intent(mission.id, ordinal=ordinal)
    assert intent.kind == "plan"
    assert intent.mission_id == mission.id
    from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
    frozen = PlanningDecisionStore(loop.store).get_mission_protocol(mission.id)
    expected = {6: PLANNER_HIERARCHICAL_V9.prompt_version, 7: PLANNER_HIERARCHICAL_V10.prompt_version}
    assert intent.config["prompt_version"] == expected[frozen["package_version"]]
    assert intent.config["planning_package"]["planning_protocol"]["protocol"] == (
        "planning-decision-v1"
    )
    return intent


def _refine_reply(package: dict[str, Any]) -> str:
    subject = package["planning_subjects"][0]["subject_key"]
    applicable = next(
        item for item in package["applicability"] if item["verdict"] == "APPLICABLE"
    )
    method = next(
        item["method_ref"]
        for item in package["method_library"]
        if item["method_ref"] == applicable["method_ref"]
    )
    method_ref = {
        "kind": "method",
        "id": method["method_id"],
        "semantic_revision": method["version"],
        "content_hash": method["content_hash"],
    }
    body = {
        "schema_version": 1,
        "decision_type": "REFINE",
        "subject_key": subject,
        "rationale": "选择当前可适用的已注册方法。",
        "reason_refs": [],
        "assumptions": [],
        "payload": {
            "method_ref": method_ref,
            "bindings": dict(package["plan"]["open_compound_goals"][0]["typed_parameters"]),
        },
        "uncertainties": [],
        "alternatives": [],
        "replan_triggers": [],
    }
    # Parsing and admission are intentionally left to the production collector.
    from agent_orchestrator.contracts.planning_decisions import PlanningDecisionEnvelopeV1

    return serialize_planning_decision(PlanningDecisionEnvelopeV1.from_json(body))


def _events(loop: Orchestrator, mission_id: str, event_type: str) -> list[Any]:
    return [event for event in loop.store.list_events(mission_id) if event.type == event_type]


def test_new_protocol_production_entry_selects_prompt_v10(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(loop, tmp_path, key="h1i-v10")
            intent = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            assert intent.config["planning_package"]["package_version"] == (
                "planner-package-hierarchical-v8"
            )
            message = intent.config["message"]["content"]
            assert (
                "rejected_method_instance and replacement_method_ref must each be JSON objects"
                in message
            )
            assert (
                "the first object has kind method_instance and the second has kind method"
                in message
            )
            assert 'decision_type exactly "REPAIR" (never "REPAIR/REPLACE_METHOD")' in message

    asyncio.run(case())


def test_format_retry_keeps_opener_package_and_request_identity(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(loop, tmp_path, key="h1i-retry")
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)

            # This is the real collector's codec refusal.  It records UNREADABLE and
            # opens the one permitted same-request format retry.
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                "<planning_decision>{not-json}</planning_decision>",
                dispatch,
            )
            opener_request = loop.store.connection.execute(
                "SELECT request_id, intent_id, package_hash, base_plan_revision "
                "FROM planning_requests WHERE request_id = ?",
                (opener.intent_id,),
            ).fetchone()
            assert opener_request is not None
            frozen_request_facts = tuple(opener_request)[2:]
            retry = await _open_planner_round(loop, mission, dispatch, ordinal=2)

            assert retry.config["planning_package"] == opener.config["planning_package"]
            assert retry.config["message"] == opener.config["message"]
            request = loop.store.connection.execute(
                "SELECT request_id, intent_id, package_hash, base_plan_revision "
                "FROM planning_requests WHERE request_id = ?",
                (opener.intent_id,),
            ).fetchone()
            assert request is not None
            assert tuple(request)[:2] == (opener.intent_id, retry.intent_id)
            assert tuple(request)[2:] == frozen_request_facts
            assert _events(loop, mission.id, "PlanningRejected")[-1].payload["reason"] == (
                "proposal_unreadable"
            )

    asyncio.run(case())


def test_malformed_decision_retry_reuses_the_frozen_request_package(tmp_path: Path) -> None:
    """A schema rejection may open one retry without changing its request facts."""

    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-retry-frozen-package"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            request_before = loop.store.connection.execute(
                "SELECT request_id, package_hash, base_plan_revision, visible_refs_digest "
                "FROM planning_requests WHERE request_id = ?",
                (opener.intent_id,),
            ).fetchone()
            assert request_before is not None
            malformed = (
                '<planning_decision>{"schema_version":1,"decision_type":"REFINE",'
                '"subject_key":"subject-root","rationale":"bad",'
                '"reason_refs":[],"assumptions":["bare-string"],'
                '"payload":{"method_ref":{"kind":"method","id":"code.fix-by-patch",'
                '"semantic_revision":2,"content_hash":"' + "0" * 64 + '"},"bindings":{}},'
                '"uncertainties":[],"alternatives":[],"replan_triggers":[]}'
                "</planning_decision>"
            )
            await loop._collect_plan_decision(
                opener, object(), mission, malformed, dispatch
            )
            request_after = loop.store.connection.execute(
                "SELECT request_id, package_hash, base_plan_revision, visible_refs_digest, "
                "intent_id "
                "FROM planning_requests WHERE request_id = ?",
                (opener.intent_id,),
            ).fetchone()
            assert request_after is not None
            assert tuple(request_after[:4]) == tuple(request_before)
            assert request_after[4] != opener.intent_id
            retry = loop.store.get_intent(request_after[4])
            assert retry is not None and retry.config["planning_package"] == opener.config[
                "planning_package"
            ]

    asyncio.run(case())


def test_commit_refusal_records_domain_code_and_not_commit_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-refusal"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id="grant-h1i-refusal",
                request_id=opener.intent_id,
            )

            def refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise PlanCommitRejected("READ_SET_STALE", "deterministic H1-I refusal")

            # Keep the real collector -> admission -> preview -> commit call chain;
            # replace only the durable CommitService refusal boundary so the regression
            # does not depend on a timing race to produce a stale commit.
            monkeypatch.setattr(CommitService, "commit_planning_revision", refuse)
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                _refine_reply(opener.config["planning_package"]),
                dispatch,
            )
            rows = _events(loop, mission.id, "PlanningDecisionEvaluated")
            assert rows[-1].payload["status"] == str(PlanningDecisionStatus.COMMIT_REJECTED)
            assert rows[-1].payload["rejection_codes"] == ["INTERNAL_CONTRACT_ERROR"]
            assert "COMMIT_REJECTED" not in rows[-1].payload["rejection_codes"]

    asyncio.run(case())


def test_real_collector_preview_commit_commits_one_plan_revision(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-commit"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id="grant-h1i-commit",
                request_id=opener.intent_id,
            )
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                _refine_reply(opener.config["planning_package"]),
                dispatch,
            )
            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")
            assert evaluated[-1].payload["status"] == str(PlanningDecisionStatus.COMMITTED)
            assert len(_events(loop, mission.id, "PlanRevisionCommitted")) == 1
            assert dispatch.network(mission.id).plan_revision == 1

    asyncio.run(case())


@pytest.mark.parametrize(
    "decision_type",
    ("WAIT", "NO_CHANGE", "DECLARE_BLOCKED"),
)
def test_state_free_decision_context_has_no_uninitialised_operation_snapshot(
    tmp_path: Path, decision_type: str
) -> None:
    """State-free decisions skip live operation reads and still assemble safely."""

    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key=f"h1i-state-free-{decision_type.lower()}"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id=f"grant-h1i-{decision_type.lower()}",
                request_id=opener.intent_id,
            )
            context = loop._hierarchical_admission_context(
                intent=opener,
                mission=mission,
                new_mode=dispatch,
                raw_text="<planning_decision>{}</planning_decision>",
                include_plan_sources=False,
            )
            assert context.operations.snapshot is None
            assert context.operations.unresolved_operations == ()

    asyncio.run(case())


def test_declare_blocked_hands_off_to_method_synthesis_without_plan_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DECLARE_BLOCKED is durable-only, then opens the existing synthesis gate."""

    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-blocked-synthesis-handoff"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id="grant-h1i-blocked-synthesis",
                request_id=opener.intent_id,
            )
            requested: list[str] = []

            async def request_synthesis(current: Any) -> bool:
                requested.append(current.id)
                return True

            monkeypatch.setattr(loop, "_request_method_synthesis", request_synthesis)
            fixture = (
                Path(__file__).parent
                / "fixtures"
                / "planning_decision_v1"
                / "valid"
                / "declare-blocked.json"
            )
            body = json.loads(fixture.read_text(encoding="utf-8"))
            body["subject_key"] = opener.config["planning_package"]["planning_subjects"][0][
                "subject_key"
            ]
            reply = serialize_planning_decision(PlanningDecisionEnvelopeV1.from_json(body))
            await loop._collect_plan_decision(
                opener, object(), mission, reply, dispatch
            )
            rows = _events(loop, mission.id, "PlanningDecisionEvaluated")
            assert rows[-1].payload["status"] == str(PlanningDecisionStatus.NO_STATE_CHANGE)
            assert requested == [mission.id]
            assert not _events(loop, mission.id, "PlanRevisionCommitted")

    asyncio.run(case())
