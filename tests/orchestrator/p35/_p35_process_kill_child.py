# ruff: noqa: E402
"""Child side of the P35 SIGKILL recovery controls.

Each scenario writes its marker only after a durable SDK/Orchestrator boundary.
The parent then kills this process without entering an async-context teardown.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]
for fixture_path in (TESTS.parent / "src", TESTS / "orchestrator" / "step07", TESTS / "agents"):
    if str(fixture_path) not in sys.path:
        sys.path.insert(0, str(fixture_path))

from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter

from agent_orchestrator.contracts import Budget
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator, OrchestratorConfig
from agent_orchestrator.runtime.assembly import PriceTable
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from agent_orchestrator.testing.fixtures import RoleScriptedProvider, envelope_step, package_of
from simple_harness.contracts import RunId


def _critic_pass(request) -> str:
    assert any(
        str(message.role) == "tool" and "P35 recovered result." in str(message.content)
        for message in request.messages
    ), "Critic must actually read the recovered artifact before giving its verdict"
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": "PASS",
                "findings": [],
                "mission_criteria": [
                    {"criterion": criterion, "met": True, "reason": "fixture artifact read"}
                    for criterion in package_of(request)["mission_success_criteria"]
                ],
            }
        )
        + "</critic_verdict>"
    )


def _scripts() -> dict[str, Sequence[object]]:
    return {
        "worker": [
            ("workspace_write_file", {"path": "a.md", "content": "P35 recovered result.\n"}),
            envelope_step(
                summary="P35 durable worker result",
                artifacts=["a.md"],
                claims=["a.md contains the P35 fixture result"],
            ),
        ],
        "critic": [("workspace_read_file", {"path": "a.md"}), _critic_pass],
    }


class ResultProvider(RoleScriptedProvider):
    """A real fixture Worker envelope plus the separate Critic verification call."""

    def __init__(self, *, gate: asyncio.Event | None = None):
        super().__init__(_scripts(), gate=gate)


class HandoffBlockedProvider(ResultProvider):
    """The SDK hands off normally; this fixture never returns response or usage."""

    def __init__(self):
        super().__init__(gate=asyncio.Event())
        self.entered = asyncio.Event()

    async def invoke(self, request, *, cancel):
        self.entered.set()
        return await super().invoke(request, cancel=cancel)


def _write_marker(path: Path, body: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(body, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _orchestrator(root: Path, provider: ResultProvider) -> Orchestrator:
    profiles = {
        name: RuntimeProfile(
            name,
            provider,
            "agent-model",
            price_table=PriceTable("original", 1_000_000, 2_000_000),
            default_max_output_tokens=1000,
            max_output_tokens_ceiling=1000,
        )
        for name in ("default", "critic")
    }
    return Orchestrator(
        OrchestratorConfig(
            evidence_root=root,
            lease_seconds=4,
            sdk_lease_ttl_seconds=1,
            attempt_reserve_tokens=4000,
        ),
        profiles=profiles,
        routing=RoutingRules("default", by_role={"critic": "critic"}),
        provider_token_estimator=Counter(100),
        poll_interval=0.005,
    )


async def _submitted_intent(orch: Orchestrator):
    mission = await orch.submit_mission(
        spec(
            conflict_reserve_tokens=0,
            success_criteria=("file:a.md",),
            budget=Budget(max_tokens=200_000, max_cost_micros=1_000_000, max_attempts=12),
        )
    )
    planning = orch.commit.begin_planning(mission.id)
    tasks, _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    node(
                        "A",
                        budget={
                            "max_tokens": 20_000,
                            "max_cost_micros": 200_000,
                            "max_attempts": 3,
                        },
                        verification_policy=["format_check", "rule_check", "critic_review"],
                    )
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "p35 process-kill control"},
    )
    # Production dispatch supplies role marker, tools, workspace and identity-bound
    # task package; a bare "answer briefly" AgentConfig cannot produce this envelope.
    assert await orch._next_attempt(orch.store.get_mission(mission.id), tasks[0], [])
    (attempt,) = orch.store.list_attempts(tasks[0].id)
    intent = orch.store.get_intent_for_subject(attempt.id)
    assert intent is not None and intent.state == "PENDING", orch.progress_log[-12:]
    assert await orch._dispatch(intent)
    intent = orch.store.get_intent(intent.intent_id)
    assert intent is not None and intent.state == "SUBMITTED"
    return intent


async def _prepare(root: Path, scenario: str, marker: Path) -> None:
    assert scenario in {"sdk-result", "handoff"}
    provider = ResultProvider() if scenario == "sdk-result" else HandoffBlockedProvider()
    orch = _orchestrator(root, provider)
    # Deliberately no context teardown: only the parent may end this child, by SIGKILL.
    await orch.__aenter__()
    intent = await _submitted_intent(orch)
    bridge = orch.bridge_for(intent)
    if scenario == "sdk-result":
        while True:
            result = await bridge.result(agent_id=intent.agent_id, turn_id=intent.expected_turn_id)
            if result is not None:
                break
            await asyncio.sleep(0.002)
        assert str(result.state) == "committed", result.error
        attempt = orch.store.get_attempt(intent.subject_id)
        envelope, _ = orch._parse_envelope(
            str(result.public_output.content), attempt, turn_id=result.turn_id
        )
        assert envelope.attempt_id == attempt.id and envelope.artifacts == ("a.md",)
        assert bridge.runtime.uow.read_agent_turn_result(intent.expected_turn_id) is not None
        assert provider.by_role == {"worker": 2}
    else:
        await provider.entered.wait()
        assert provider.by_role == {"worker": 1}
        assert bridge.runtime.uow.read_agent_turn_result(intent.expected_turn_id) is None
    records = bridge.runtime.uow.list_provider_invocations(RunId(intent.agent_id))
    expected_state = "succeeded" if scenario == "sdk-result" else "handed_off"
    assert len(records) == (2 if scenario == "sdk-result" else 1)
    assert all(str(r.state) == expected_state and r.handoff_attempt == 1 for r in records)
    assert all(r.rehandoff_count == 0 for r in records)
    if scenario == "handoff":
        assert records[0].response_json is None
        usage = records[0].usage_json
        assert isinstance(usage, Mapping) and usage.get("usage") is None
        # The SDK persists a provisional budget here; that is not actual usage.
    assert orch.store.find_result_for_attempt(intent.subject_id) is None
    assert orch.store.get_intent(intent.intent_id).state == "SUBMITTED"
    attempt = orch.store.get_attempt(intent.subject_id)
    _write_marker(
        marker,
        {
            "point": (
                "sdk-result-before-orchestrator-collect"
                if scenario == "sdk-result"
                else "provider-handoff-without-usage"
            ),
            "intent_id": intent.intent_id,
            "subject_id": intent.subject_id,
            "agent_id": intent.agent_id,
            "turn_id": intent.expected_turn_id,
            "invocation_ids": [r.invocation_id for r in records],
            "original_worker_calls": provider.by_role["worker"],
            "lease_expires_at": attempt.lease_expires_at,
            "original_owner": attempt.lease_owner,
        },
    )


async def main(root: Path, scenario: str, marker: Path) -> None:
    await asyncio.wait_for(_prepare(root, scenario, marker), timeout=10)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
