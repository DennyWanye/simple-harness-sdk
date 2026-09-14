"""Actual S/R BaseAgents and D/F Orchestrators over one shared AppWorld gateway.

The caller supplies one metered provider for the whole episode and scores only
after this coroutine exits. No arm can call the external evaluator. R retains a
single Agent identity and selects a saved candidate from its own observations.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_harness.agents import AgentConfig, AgentLimits, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization

from ..artifacts.workspace import WorkspaceManager
from ..contracts import Budget
from ..governance.policies import DeploymentPolicy
from ..orchestrator.commit_service import MissionSpec
from ..orchestrator.event_handler import Orchestrator
from ..runtime.assembly import OrchestratorConfig
from ..runtime.model_router import RuntimeProfile
from ..runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway, read_tool_schemas
from .appworld import AppWorldEpisode
from .experiment import ExperimentBudget

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "appworld_execute")
SELF_SELECTION_PROTOCOL_VERSION = "appworld-r-self-selection-v1"
SELF_SELECTION_MAX_CHARS = 16_384
PUBLIC_GUIDANCE = (
    "Operate the user's AppWorld task through appworld_execute(code). The Python shell and "
    "application state persist. Use print to observe results. Discover public APIs with "
    "apis.api_docs.show_app_descriptions(), "
    "apis.api_docs.show_api_descriptions(app_name='...'), and "
    "apis.api_docs.show_api_doc(app_name='...', api_name='...'). "
    "Read required parameters before calling an unfamiliar API. Use the supervisor APIs "
    "to discover the simulated user's accounts. Check errors and actual effects; do not "
    "repeat successful mutations without checking state. Solve every part of the user's "
    "request, then call apis.supervisor.complete_task() only when the entire goal is met. "
    "You have no access to hidden answers or the benchmark evaluator. Workspace files "
    "are reports, not application databases. Record what really happened in REPORT.md, "
    "including uncompleted items. Tool observations establish only their stated scope."
)


@dataclass(frozen=True, slots=True)
class ArmRuntime:
    provider: Any  # The exact same metered instance serves every role/attempt.
    model: str
    tokenizer: Any
    context_policy: Any
    budget: ExperimentBudget
    physical_slots: int = 1
    default_output_tokens: int = 8192
    maximum_output_tokens: int = 32768
    repetitions: int = 2


async def execute_arm(
    arm: str, episode: AppWorldEpisode, runtime: ArmRuntime, root: Path
) -> dict[str, Any]:
    if arm not in {"S", "R", "D", "F"}:
        raise ValueError("arm must be S/R/D/F")
    if root.exists():
        raise FileExistsError("arm evidence must be fresh; never replace a failed episode")
    root.mkdir(parents=True)
    if arm in {"S", "R"}:
        return await _single_identity(arm, episode, runtime, root)
    return await _orchestrated(arm, episode, runtime, root)


async def _single_identity(
    arm: str, episode: AppWorldEpisode, config: ArmRuntime, root: Path
) -> dict[str, Any]:
    worlds = WorkspaceManager(root / "workspaces")
    world = worlds.create("single-agent", seed={})
    gateway = WorkspaceToolGateway(
        worlds, local_code_execution=False, appworld_execute=episode.agent.execute
    )
    gateway.bind_appworld("episode")
    ports = AgentRuntimePorts(
        provider=config.provider,
        authorization=AllowAllAuthorization(),
        database_path=str(root / "execution.db"),
        tool_executor=gateway,
        tool_names=TOOLS,
        tool_schemas=read_tool_schemas(large=True),
        model=config.model,
        context_policy=config.context_policy,
        tokenizer=config.tokenizer,
        max_concurrent_model_calls=config.physical_slots,
        default_max_output_tokens=config.default_output_tokens,
        max_output_tokens_ceiling=config.maximum_output_tokens,
    )
    results: list[dict[str, Any]] = []
    candidates: list[str] = []
    selected: int | None = None
    repeats = 1 if arm == "S" else config.repetitions
    initial = episode.checkpoint() if arm == "R" else None
    calls_per_turn = (
        config.budget.calls if arm == "S" else max(1, config.budget.calls // (repeats + 1))
    )
    try:
        async with build_agent_runtime(ports, owner_scope="appworld-evaluation") as runtime:
            agent = await runtime.create(
                AgentConfig(
                    name=f"{arm}-single-identity",
                    instructions=PUBLIC_GUIDANCE,
                    model_profile_ref="default",
                    tool_names=TOOLS,
                    limits=AgentLimits(
                        max_model_calls_per_turn=calls_per_turn,
                        max_tool_calls_per_turn=config.budget.calls * 3,
                        turn_deadline_seconds=config.budget.seconds,
                        lifetime_model_calls=config.budget.calls,
                        lifetime_wall_seconds=config.budget.seconds,
                    ),
                ),
                creation_key="only-agent",
            )
            binding = WorkspaceBinding(
                "single-agent",
                "work",
                True,
                TOOLS,
                context_policy=config.context_policy,
                tokenizer=config.tokenizer,
                mission_id="episode",
            )
            gateway.bind(agent.run_id, binding)
            for index in range(repeats):
                if index:
                    assert initial is not None
                    episode.restore(initial)
                    # Reports are per candidate; the same Agent retains its prior
                    # reasoning/observations but the world starts from the same DBs.
                    for path in world.root.rglob("*.md"):
                        path.unlink()
                prompt = episode.agent.instruction
                if arm == "R":
                    prompt += (
                        f"\nCandidate {index + 1}/{repeats}. The world is at its original state. "
                        "Use previous observations to improve this independent attempt. "
                        "There will be a separate self-selection turn; "
                        "no hidden scores are provided."
                    )
                receipt = await agent.submit(prompt, input_id=f"candidate-{index}")
                outcome = await agent.wait_turn(receipt.turn_id, timeout=config.budget.seconds)
                results.append(outcome.to_json())
                if arm == "R":
                    candidates.append(episode.checkpoint())
                (root / "turns.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            if arm == "R":
                # Selection gets no new world actions, answers or evaluator feedback.
                gateway.bind(
                    agent.run_id,
                    WorkspaceBinding("single-agent", "work", False, (), mission_id="episode"),
                )
                receipt = await agent.submit(
                    f"Select the best of your {repeats} candidates "
                    "using only your own observations. "
                    "Do not call tools. You may explain your choice before one selection envelope, "
                    "but output exactly one JSON object with no other JSON objects: "
                    '{"protocol_version":"appworld-r-self-selection-v1","selected_candidate":N}, '
                    f"where N is a 1-based integer from 1 to {repeats}. "
                    "Do not invent hidden evaluation results.",
                    input_id="self-selection",
                )
                choice = await agent.wait_turn(receipt.turn_id, timeout=config.budget.seconds)
                results.append(choice.to_json())
                (root / "turns.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
                if choice.public_output is None:
                    raise ValueError("self-selection has no output")
                selected = _parse_self_selection(choice.public_output.content, repeats)
                episode.restore(candidates[selected - 1])
            return {
                "arm": arm,
                "agent_ids": [agent.agent_id],
                "turns": len(results),
                "selected_candidate": selected,
                "runtime_states": [r["state"] for r in results],
                "tool_calls": len(gateway.calls),
            }
    finally:
        (root / "gateway.json").write_text(json.dumps(gateway.calls, ensure_ascii=False, indent=2))


def _parse_self_selection(content: str, repeats: int) -> int:
    """Accept one versioned selection envelope, optionally after prose.

    A response can contain an explanation prefix, but more than one JSON object
    is ambiguous.  The envelope is closed so an older or expanded protocol
    cannot silently change which candidate is restored.
    """
    if not isinstance(content, str) or len(content) > SELF_SELECTION_MAX_CHARS:
        raise ValueError("self-selection output must be bounded text")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate self-selection field")
            result[key] = value
        return result

    start = content.find("{")
    if start < 0:
        raise ValueError("self-selection requires exactly one JSON envelope")
    try:
        envelope, end = json.JSONDecoder(object_pairs_hook=unique_object).raw_decode(content, start)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid self-selection JSON envelope") from error
    if content[end:].strip() not in {"", "```"}:
        raise ValueError("self-selection requires exactly one final JSON envelope")
    if set(envelope) != {"protocol_version", "selected_candidate"}:
        raise ValueError("invalid self-selection envelope fields")
    if envelope["protocol_version"] != SELF_SELECTION_PROTOCOL_VERSION:
        raise ValueError("unsupported self-selection protocol version")
    selected = envelope["selected_candidate"]
    if type(selected) is not int or not 1 <= selected <= repeats:
        raise ValueError("invalid self-selected candidate")
    return selected


async def _orchestrated(
    arm: str, episode: AppWorldEpisode, config: ArmRuntime, root: Path
) -> dict[str, Any]:
    budget = config.budget
    # D and F both use real Planner/Worker/Critic roles. D freezes its initial
    # graph; F enables normal graph management. All role overhead uses one meter.
    cfg = OrchestratorConfig(
        evidence_root=root / "orchestrator",
        model=config.model,
        max_concurrency=3,
        max_concurrent_model_calls=config.physical_slots,
        dynamic_graph=arm == "F",
        knowledge_sharing=True,
        default_max_output_tokens=config.default_output_tokens,
        max_output_tokens_ceiling=config.maximum_output_tokens,
        max_model_calls_per_turn=budget.calls,
        max_tool_calls_per_turn=budget.calls * 3,
        turn_deadline_seconds=budget.seconds,
        stall_seconds=min(300, budget.seconds),
        attempt_reserve_tokens=min(100000, budget.total_tokens // 3),
        planner_reserve_tokens=min(50000, budget.total_tokens // 5),
        critic_reserve_tokens=min(50000, budget.total_tokens // 5),
        appworld_execute=episode.agent.execute,
        deployment_policy=DeploymentPolicy(allowed_tools=TOOLS, local_code_execution=False),
    )
    profile = RuntimeProfile(
        "default",
        config.provider,
        config.model,
        provider_kind="env",
        context_policy=config.context_policy,
        tokenizer=config.tokenizer,
        default_max_output_tokens=config.default_output_tokens,
        max_output_tokens_ceiling=config.maximum_output_tokens,
        max_concurrent_model_calls=config.physical_slots,
    )
    async with Orchestrator(
        cfg, profiles={"default": profile}, provider_token_estimators={"default": config.tokenizer}
    ) as orch:
        mission = await orch.submit_mission(
            MissionSpec(
                goal=episode.agent.instruction + "\n" + PUBLIC_GUIDANCE,
                success_criteria=("file:REPORT.md",),
                tenant_id="appworld-evaluation",
                idempotency_key=episode.config.experiment_name,
                allowed_tools=TOOLS,
                budget=Budget(
                    max_tokens=budget.total_tokens,
                    max_attempts=12,
                    max_runtime_seconds=int(budget.seconds),
                ),
                domain="appworld-v1",
                runtime_profile_id=profile.profile_id,
            )
        )
        try:
            async with asyncio.timeout(budget.seconds):
                await orch.run()
        finally:
            (root / "gateway.json").write_text(
                json.dumps(orch.assembled.gateway.calls, ensure_ascii=False, indent=2)
            )
        current = orch.store.get_mission(mission.id)
        assert current is not None
        tasks = orch.store.list_tasks(mission.id)
        return {
            "arm": arm,
            "mission_id": mission.id,
            "mission_status": str(current.status),
            "stop_reason": current.stop_reason,
            "tasks": [{"id": t.id, "status": str(t.status)} for t in tasks],
            "tool_calls": len(orch.assembled.gateway.calls),
            "dynamic_graph": arm == "F",
        }
