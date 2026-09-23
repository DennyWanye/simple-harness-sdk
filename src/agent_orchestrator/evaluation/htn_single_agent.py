# SPDX-License-Identifier: Apache-2.0
"""Strong single-Agent H8 arm over the same deployed gateway and physical meter."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_harness.agents import AgentConfig, AgentLimits, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import canonical_json
from ..artifacts.workspace import WorkspaceManager
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..runtime.assembly import OrchestratorConfig
from ..runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway, read_tool_schemas
from .htn_matrix import H8Manifest, H8Run
from .metered_provider import MeteredProvider


@dataclass(frozen=True, slots=True)
class SingleAgentExecution:
    workspace: Path
    agent_id: str
    run_id: str
    turn_id: str
    outcome: Mapping[str, Any]
    tools_hash: str


async def execute_single_agent(*, manifest: H8Manifest, run: H8Run, config: OrchestratorConfig,
        provider: MeteredProvider, root: Path, prompt: str, seed: Mapping[str, str],
        allowed_tools: tuple[str, ...], tokenizer: Any, context_policy: Any,
        instructions: str, resume: bool = False,
        on_tick: Callable[[Any, Any, Path], Awaitable[None]] | None = None) -> SingleAgentExecution:
    if (provider.target.provider_id, provider.target.model) != (manifest.provider, manifest.model):
        raise ContractError("strong single arm must use the frozen physical provider/model")
    if config.model != manifest.model or config.max_concurrent_model_calls != manifest.physical_slots:
        raise ContractError("single-Agent deployment differs from the frozen model/concurrency")
    if (root / "execution.db").exists() and not resume:
        raise ContractError("an existing single-Agent runtime requires explicit recovery")
    schemas = {**read_tool_schemas(large=context_policy is not None),
               **{name: tool.schema for name, tool in config.domain_tools.items()}}
    try:
        exposed = {name: schemas[name] for name in allowed_tools}
    except KeyError as error:
        raise ContractError("single-Agent arm names an undeployed tool") from error
    tools_hash = content_hash_of(exposed)
    if tools_hash != content_hash_of(manifest.domain_deployments[run.scenario.domain]["tool_schemas"]):
        raise ContractError("single-Agent and hierarchical arms must expose identical tools")
    identity: dict[str, Any] = {"run_id": run.run_id, "manifest_hash": manifest.fingerprint,
                "prompt": prompt, "instructions": instructions, "seed": dict(seed), "tools_hash": tools_hash}
    root.mkdir(parents=True, exist_ok=True)
    binding_file = root / "single-binding.json"
    frozen = canonical_json(identity)
    if binding_file.exists() and binding_file.read_text() != frozen:
        raise ContractError("single-Agent input changed during recovery")
    binding_file.write_text(frozen)
    workspaces = WorkspaceManager(root / "workspaces")
    workspace = workspaces.create("single-agent", seed=seed)
    from ..runtime.sandbox import resolve_executor
    gateway = WorkspaceToolGateway(workspaces, test_timeout=config.test_timeout_seconds,
        local_code_execution=config.deployment_policy.local_code_execution,
        executor=resolve_executor(config.deployment_policy, config.sandbox_executor), appworld_execute=config.appworld_execute,
        domain_tools=config.domain_tools)
    if config.appworld_execute is not None:
        gateway.bind_appworld(run.run_id)
    ports = AgentRuntimePorts(provider=provider, authorization=AllowAllAuthorization(),
        database_path=str(root / "execution.db"), tool_executor=gateway,
        tool_names=allowed_tools, tool_schemas=exposed, model=manifest.model,
        tokenizer=tokenizer, context_policy=context_policy,
        max_concurrent_model_calls=manifest.physical_slots,
        default_max_output_tokens=config.default_max_output_tokens,
        max_output_tokens_ceiling=config.max_output_tokens_ceiling)
    total_tool_budget = config.max_tool_calls_per_turn * manifest.budget.calls
    async with build_agent_runtime(ports, owner_scope="h8-strong-single-agent") as runtime:
        agent = await runtime.create(AgentConfig(name="h8-strong-single-agent", instructions=instructions,
            model_profile_ref="default", tool_names=allowed_tools,
            limits=AgentLimits(max_model_calls_per_turn=manifest.budget.calls,
                max_tool_calls_per_turn=total_tool_budget,
                lifetime_tool_calls=total_tool_budget,
                turn_deadline_seconds=manifest.budget.seconds,
                lifetime_model_calls=manifest.budget.calls, lifetime_wall_seconds=manifest.budget.seconds)),
            creation_key=run.run_id)
        gateway.bind(agent.run_id, WorkspaceBinding("single-agent", "work", True, allowed_tools,
            max_tool_calls=total_tool_budget, protected=tuple(run.scenario.oracle.get("immutable_files", {})),
            tokenizer=tokenizer, context_policy=context_policy, mission_id=run.run_id))
        if on_tick is not None:
            await on_tick(gateway, agent, workspace.root)
        submitted = await agent.submit(prompt, input_id="h8-task")
        if resume:
            await runtime.recover_pending_turns()
        # Poll the original durable result; this does not submit another input or
        # create a fresh turn. The physical meter owns the recovery deadline.
        while (outcome := agent.get_result(submitted.turn_id)) is None:
            if time.monotonic() >= provider._deadline:
                return SingleAgentExecution(workspace.root, agent.agent_id, agent.run_id,
                    submitted.turn_id, {"state": "timeout"}, tools_hash)
            if on_tick is not None:
                await on_tick(gateway, agent, workspace.root)
            await asyncio.sleep(0.01)
        if on_tick is not None:
            await on_tick(gateway, agent, workspace.root)
        return SingleAgentExecution(workspace.root, agent.agent_id, agent.run_id, submitted.turn_id,
                                    outcome.to_json(), tools_hash)
