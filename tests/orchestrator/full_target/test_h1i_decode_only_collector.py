"""I01: the real collector journals the two 2026-09-19 decode-only decisions.

The amendment demoted only ``BIND_EXISTING_GOAL`` and
``REPAIR/PROPOSE_SUCCESSOR``.  They are still strict wire inputs, but H1 must
write their exact raw bytes and a terminal phase refusal before it touches
authority, preview, or plan commit machinery.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_h1i_production_entry import _config, _events, _open_planner_round, _seed_new_protocol

from agent_orchestrator.contracts.planning_decisions import (
    PlanningDecisionStatus,
    PlanningDecisionType,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.decision_codec import hash_raw_output, parse_planning_decision
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

_FIXTURES = (
    # The two rows specifically demoted by the 2026-09-19 15:20 amendment.
    ("repair-propose-successor", "REPAIR/PROPOSE_SUCCESSOR", PlanningDecisionType.REPAIR),
    ("bind-existing-goal-reuse", "BIND_EXISTING_GOAL", PlanningDecisionType.BIND_EXISTING_GOAL),
)
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "planning_decision_v1" / "valid"


def _raw_fixture(name: str, package: dict[str, Any]) -> str:
    """Keep fixture payload syntax, grounding only the real request's subject."""

    body = json.loads((_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    body["subject_key"] = package["planning_subjects"][0]["subject_key"]
    return "<planning_decision>" + json.dumps(body, ensure_ascii=False) + "</planning_decision>"


@pytest.mark.parametrize(("fixture_name", "phase_key", "decision_type"), _FIXTURES)
def test_decode_only_collector_persists_raw_then_refuses_without_admission_or_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    phase_key: str,
    decision_type: PlanningDecisionType,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            # Create under the historical deployment, then collect under today's
            # deployment. Existing Missions must retain their decode-only phase;
            # new package-7 Missions exercise the H4 compiler separately.
            from agent_orchestrator.orchestrator import planning_protocol_binding
            with monkeypatch.context() as historical:
                historical.setattr(planning_protocol_binding, "PLANNING_PROTOCOL_BINDING",
                                   {"package_version": 6, "prompt_version": "planner-hierarchical-v9"})
                mission, _env, _contract, dispatch = _seed_new_protocol(
                    loop, tmp_path, key=f"i01-{fixture_name}"
                )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            raw = _raw_fixture(fixture_name, opener.config["planning_package"])

            # This proves the input reached the production strict codec; the fixture
            # remains a valid envelope even though its payload refs are not manufactured
            # into a synthetic AdmissionContext.
            decoded = parse_planning_decision(raw)
            assert decoded.decision_type is decision_type
            assert PlanningAdmissionStore(loop.store).get_request_binding(opener.intent_id) is None

            def forbidden(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("decode-only collector must not enter this producer")

            # The admission-context builder is where the planning-authority reader is
            # reached.  Preview and commit are independently trapped so a phase-only
            # rejection cannot silently become a proposal/commit regression.
            monkeypatch.setattr(loop, "_hierarchical_admission_context", forbidden)
            monkeypatch.setattr(dispatch, "preview_plan_proposal", forbidden)
            monkeypatch.setattr(dispatch, "commit_preview_plan_proposal", forbidden)

            request_count = loop.store.connection.execute(
                "SELECT COUNT(*) FROM planning_requests WHERE mission_id = ?", (mission.id,)
            ).fetchone()[0]
            committed_before = len(_events(loop, mission.id, "PlanRevisionCommitted"))
            action_count = loop.store.connection.execute(
                "SELECT COUNT(*) FROM actions WHERE mission_id = ?", (mission.id,)
            ).fetchone()[0]

            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)

            row = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert row is not None
            assert row["status"] == str(PlanningDecisionStatus.REJECTED)
            assert row["decision_type"] == str(decision_type)
            assert row["rejection_codes"] == ["DECISION_NOT_ENABLED_IN_PHASE"]
            assert row["raw_output_hash"] == hash_raw_output(raw.encode("utf-8"))
            assert row["raw_artifact_ref"]
            assert loop.assembled.workspaces.artifact_store.read(
                row["raw_artifact_ref"]
            ) == raw.encode("utf-8")

            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")
            assert len(evaluated) == 1
            assert evaluated[0].payload["status"] == str(PlanningDecisionStatus.REJECTED)
            assert evaluated[0].payload["rejection_codes"] == ["DECISION_NOT_ENABLED_IN_PHASE"]
            assert phase_key in evaluated[0].payload["detail"]["problems"][0]["detail"]

            # The phase refusal ends exactly this intent.  It neither binds authority
            # nor opens the normal formatting/rejection ladder, nor can it mutate the
            # plan/action surface.
            assert loop.store.get_intent(opener.intent_id).state == "FAILED"
            assert PlanningAdmissionStore(loop.store).get_request_binding(opener.intent_id) is None
            assert (
                loop.store.connection.execute(
                    "SELECT COUNT(*) FROM planning_requests WHERE mission_id = ?", (mission.id,)
                ).fetchone()[0]
                == request_count
            )
            assert len(_events(loop, mission.id, "PlanRevisionCommitted")) == committed_before
            assert (
                loop.store.connection.execute(
                    "SELECT COUNT(*) FROM actions WHERE mission_id = ?", (mission.id,)
                ).fetchone()[0]
                == action_count
            )

    asyncio.run(case())
