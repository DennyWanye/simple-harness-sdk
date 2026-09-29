# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 §11 / §12.1 E8: a Mission's Worker uses the deployment's Skills.

The three model-side Skill tools (discover / load / execute) are served by a native-plane
pool's own runtime, not the orchestrator's tool gateway.  A deployment that runs such
pools declares them (``DeploymentPolicy.skill_tools``); the current hierarchical Worker
role lists them; the original Mission ∩ Task ∩ Role ∩ Deployment intersection then freezes
them into the Worker's request — and only on a pool that serves them.  Every use asks the
catalogue again, so a Skill suspended between two calls of one turn is refused by name.

Real Orchestrator, real strict-graph enable, real Worker dispatch; only the model replies
are scripted and the Skill is admitted through the test acceptance double.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parents[3]
for _extra in (_TESTS / "agents", _TESTS / "agents" / "arp"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import production_fixture  # noqa: E402
import test_h1i_production_entry as entry  # noqa: E402
from arp_fixture import ExactWordTokenizer, RecordingAuthorization, activation_receipt, meter_binding, standalone_profile, trusted_caller  # noqa: E402
from skill_fixture import AcceptingAssurance, admit_skill, import_skill, lifecycle_command, md_bundle  # noqa: E402
from test_execution_view import _run_worker  # noqa: E402

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi  # noqa: E402
from agent_orchestrator.governance.permissions import Principal  # noqa: E402
from agent_orchestrator.governance.policies import SKILL_TOOL_NAMES, DeploymentPolicy, effective_tools  # noqa: E402
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.taskgraph_assembly import TaskGraphDeploymentPorts  # noqa: E402
from agent_orchestrator.orchestrator.taskgraph_deployment import InstalledHtnWiringAcceptance  # noqa: E402
from agent_orchestrator.runtime import role_templates as roles  # noqa: E402
from agent_orchestrator.runtime.model_router import RuntimeProfile as PoolProfile  # noqa: E402
from agent_orchestrator.runtime.native_plane import NativePlaneAssembly, intent_caller  # noqa: E402
from agent_orchestrator.runtime.tool_gateway import TOOL_NAMES  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider, envelope_step, package_of  # noqa: E402
from simple_harness.agents.arp import store as arp_store  # noqa: E402
from simple_harness.agents.arp.pins import Pin  # noqa: E402
from simple_harness.agents.arp.ports import ArpPorts, bootstrap_root  # noqa: E402
from simple_harness.agents.arp.skill_tools import SKILL_DISCOVER_TOOL_NAME, SKILL_EXECUTE_TOOL_NAME, SKILL_LOAD_TOOL_NAME  # noqa: E402
from simple_harness.agents.context.budget import ContextPolicy  # noqa: E402

SKILLS = (SKILL_DISCOVER_TOOL_NAME, SKILL_LOAD_TOOL_NAME, SKILL_EXECUTE_TOOL_NAME)
OWNER = Pin("policy", "owner-skill-pool", 1, "0" * 64)


# ---- the deployment declaration and the role ------------------------------------------------


def test_a_deployment_declares_the_skill_tools_its_pools_serve():
    assert SKILL_TOOL_NAMES == SKILLS
    # naming a Skill tool without declaring it is an unknown tool, as before
    with pytest.raises(ValueError):
        DeploymentPolicy(allowed_tools=(*TOOL_NAMES, SKILL_EXECUTE_TOOL_NAME))
    # only the three model-side Skill tools can be declared
    with pytest.raises(ValueError):
        DeploymentPolicy(skill_tools=("tool_discover",))
    declared = DeploymentPolicy(allowed_tools=(*TOOL_NAMES, *SKILLS), skill_tools=SKILLS)
    assert declared.to_json()["skill_tools"] == list(SKILLS)
    # an existing deployment keeps its exact document (no new key)
    assert "skill_tools" not in DeploymentPolicy().to_json()


def test_the_current_hierarchical_worker_lists_the_skill_tools_and_older_versions_stay_replayable():
    current = roles.WORKER_HIERARCHICAL
    previous = roles.TEMPLATE_VERSIONS["worker"]["worker-hierarchical-v4"]
    assert current.prompt_version == "worker-hierarchical-v5"
    assert current.tool_names == (*previous.tool_names, *SKILLS)
    assert current.instructions.startswith(previous.instructions) and SKILL_DISCOVER_TOOL_NAME in current.instructions
    assert {"worker-hierarchical-v4", "worker-hierarchical-v5"} <= roles.HIERARCHICAL_WORKER_VERSIONS
    # the drone-sim Worker is a frozen version and keeps its tools exactly
    drone = roles.TEMPLATE_VERSIONS["worker"]["worker-drone-sim-hierarchical-v1"]
    assert drone.tool_names == (*previous.tool_names, "drone_sim_telemetry", "drone_sim_command")
    # the original intersection: offered by the deployment → the Worker gets them
    offered = DeploymentPolicy(allowed_tools=(*TOOL_NAMES, *SKILLS), skill_tools=SKILLS)
    tools = (*TOOL_NAMES, *SKILLS)
    assert set(SKILLS) <= set(effective_tools(mission_tools=tools, task_tools=tools, role_tools=current.tool_names, deployment=offered))
    assert not set(SKILLS) & set(effective_tools(mission_tools=TOOL_NAMES, task_tools=TOOL_NAMES,
                                                  role_tools=current.tool_names, deployment=DeploymentPolicy()))


# ---- the real dispatch path ------------------------------------------------------------------


def _native_pool(provider, tmp_path: Path):  # type: ignore[no-untyped-def]
    tokenizer = ExactWordTokenizer()

    def arp_ports(execution_db: Path) -> ArpPorts:
        root = bootstrap_root(execution_db.parent / "arp-root", root_id="root-skill-test")
        return ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt=activation_receipt(),
                        meter=meter_binding(tokenizer, input_limit=400_000, max_output=131_072), acceptance=AcceptingAssurance())

    native = NativePlaneAssembly(arp_ports=arp_ports, authorization=RecordingAuthorization(),
                                 caller_for=lambda intent: intent_caller(intent, principal_id="host:user", owner_contract_ref=OWNER))
    return PoolProfile("default", provider, "agent-model", context_policy=ContextPolicy(), tokenizer=tokenizer, native_plane=native)


@asynccontextmanager
async def _skill_world(tmp_path: Path, monkeypatch, *, worker_steps, native: bool):  # type: ignore[no-untyped-def]
    """``production_fixture.enabled_world`` with a deployment that offers the Skill tools
    and a Mission charter that allows them; the pool is native or the legacy one."""
    plain_spec = entry.MissionSpec
    monkeypatch.setattr(entry, "MissionSpec", lambda **kw: plain_spec(**{**kw, "allowed_tools": (*kw["allowed_tools"], *SKILLS)}))
    deployment = DeploymentPolicy(allowed_tools=(*TOOL_NAMES, *SKILLS), skill_tools=SKILLS)
    config = replace(entry._config(tmp_path), deployment_policy=deployment)
    reader = InstalledHtnWiringAcceptance()
    reader._read()
    provider = RoleScriptedProvider({"planner": [lambda request: entry._refine_reply(package_of(request))],
                                     "worker": list(worker_steps)})
    extra = {"profiles": {"default": _native_pool(provider, tmp_path)}} if native else {}
    async with Orchestrator(config, provider, **extra) as loop:
        mission, _env, _binding, dispatch = entry._seed_new_protocol(loop, tmp_path, key="tg-worker-skills")
        principal = Principal(loop._owner)
        graph = loop.install_taskgraph(TaskGraphDeploymentPorts(
            tenant_id=mission.tenant_id, principal=principal,
            deployment_acceptance=reader, graph_budget=DEFAULT_PROJECTION_BUDGET))
        intent = await entry._open_planner_round(loop, mission, dispatch, ordinal=1)
        authorization = PlanningAuthorizationApi(loop.commit, tenant_id=mission.tenant_id, principal=principal)
        grant = authorization.issue(mission.id, command_id="grant:skills", request_id=intent.intent_id)
        graph.policy.enable_taskgraph_contract(mission.id, "enable:skills")
        yield production_fixture.ProductionWorld(loop, mission, dispatch, graph, provider, intent, authorization, grant)


def _worker_intent(world):  # type: ignore[no-untyped-def]
    [intent_id] = [r[0] for r in world.loop.store.connection.execute(
        "SELECT intent_id FROM dispatch_intents WHERE kind='attempt' AND mission_id=?", (world.mission.id,))]
    return world.loop.store.get_intent(intent_id)


def test_a_mission_worker_discovers_and_executes_an_admitted_skill_and_a_suspension_refuses_the_next_use(tmp_path, monkeypatch):
    seen: dict = {}

    def discover(request):  # type: ignore[no-untyped-def]
        seen["offered"] = sorted(t.name for t in request.tools)
        return (SKILL_DISCOVER_TOOL_NAME, {"limit": 8})

    def execute(request):  # type: ignore[no-untyped-def]
        return (SKILL_EXECUTE_TOOL_NAME, {"skill_ref": seen["pin"], "arguments": {}})

    def suspend_then_execute(request):  # type: ignore[no-untyped-def]
        runtime = seen["runtime"]
        runtime.arp.lifecycle.transition(lifecycle_command(runtime, seen["revision"], "SUSPEND", acceptance=None),
                                         caller=seen["caller"], command_id="suspend-1")
        return (SKILL_EXECUTE_TOOL_NAME, {"skill_ref": seen["pin"], "arguments": {}})

    steps = (
        discover, execute, suspend_then_execute,
        ("workspace_write_file", {"path": "facts.md", "content": "Repository facts."}),
        envelope_step(summary="Recorded repository facts.", artifacts=["facts.md"], claims=[],
                      override=lambda value: {**value, "outputs": {"facts": "facts.md"}}),
    )

    async def case():  # type: ignore[no-untyped-def]
        async with _skill_world(tmp_path, monkeypatch, worker_steps=steps, native=True) as world:
            runtime = world.loop.assembled.pool("default").runtime
            revision = import_skill(runtime, md_bundle("repo-notes", "记录仓库事实时先列出文件。"), command="install-1").revision
            admit_skill(runtime, revision, command="admit-1")
            seen.update(runtime=runtime, revision=revision, pin=revision.pin.to_json(), caller=trusted_caller())
            await world.commit_seed()
            await _run_worker(world)

            intent = _worker_intent(world)
            assert set(SKILLS) <= set(intent.config["allowed_tools"])
            assert set(SKILLS) <= set(intent.config["agent_config"]["tool_names"])
            assert set(SKILLS) <= set(seen["offered"])
            connection = runtime.uow.database.connection
            [session_id] = [r[0] for r in connection.execute("SELECT session_id FROM arp_agent_sessions WHERE agent_id=?", (intent.agent_id,))]
            uses = arp_store.read_skill_uses(connection, session_id)
            assert [u["mode"] for u in uses] == ["INSTRUCTIONS"]  # the suspended call bound no use
            texts = [str(m.content) for m in world.provider.requests[-1].messages]
            assert any("记录仓库事实时先列出文件" in t for t in texts)  # the executed instructions reached the model
            assert any("SKILL_NOT_ADMITTED" in t for t in texts), texts[-3:]  # re-checked at the next use

    asyncio.run(case())


def test_a_legacy_pool_never_freezes_skill_tools_it_cannot_serve(tmp_path, monkeypatch):
    steps = (
        ("workspace_write_file", {"path": "facts.md", "content": "Repository facts."}),
        envelope_step(summary="Recorded repository facts.", artifacts=["facts.md"], claims=[],
                      override=lambda value: {**value, "outputs": {"facts": "facts.md"}}),
    )

    async def case():  # type: ignore[no-untyped-def]
        async with _skill_world(tmp_path, monkeypatch, worker_steps=steps, native=False) as world:
            await world.commit_seed()
            await _run_worker(world)
            intent = _worker_intent(world)
            assert not set(SKILLS) & set(intent.config["allowed_tools"])
            assert intent.config["prompt_version"] == "worker-hierarchical-v5"
            assert world.loop.store.connection.execute(
                "SELECT COUNT(*) FROM results WHERE mission_id=? AND verification_state='DONE'", (world.mission.id,)).fetchone()[0] >= 1

    asyncio.run(case())
