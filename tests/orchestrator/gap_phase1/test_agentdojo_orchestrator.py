"""Real SDK Mission, official optional runtime, harmless synthetic tools only."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from functools import wraps
from hashlib import sha256
from threading import Event
from types import SimpleNamespace
from typing import Annotated

import pytest
from pydantic import BaseModel, ConfigDict

from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.contracts import Claim, ClaimStatus
from agent_orchestrator.evaluation.agentdojo_bridge import (
    AgentDojoToolPort,
    make_agentdojo_pipeline,
)
from agent_orchestrator.evaluation.agentdojo_knowledge import (
    SYSTEM_PROPOSER,
    AgentDojoToolReceipt,
    make_agentdojo_tool_knowledge,
)
from agent_orchestrator.evaluation.agentdojo_runner import (
    RUNTIME_TERMINATION_BOUNDARY,
    SCHEMA_ADAPTER_VERSION,
    AdapterArgumentsError,
    AgentDojoRunner,
    AgentDojoSchemaAdapter,
    runtime_tool_schemas,
)
from agent_orchestrator.evaluation.experiment import (
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
    RunContext,
)
from agent_orchestrator.governance.domains import AGENTDOJO_PROFILE, resolve_domain
from agent_orchestrator.governance.policies import DeploymentPolicy, policy_snapshot
from agent_orchestrator.memory.blackboard import Blackboard
from agent_orchestrator.memory.verified_knowledge import KnowledgeRecord
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain
from agent_orchestrator.runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    package_of,
)
from agent_orchestrator.verification.domain_handlers import handler_for
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer
from simple_harness.contracts import CallId
from simple_harness.providers import ProviderTarget
from simple_harness.tools import ToolCall
from simple_harness.tools.schema import validate_tool_schema


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class Counter:
    fingerprint = "synthetic-agentdojo-counter-v1"
    bound_protocol = "synthetic-fixed-v1"
    requires_prior_output_reserve = False

    def estimate_input_tokens(self, request):
        return 8000


def context(reports=None, slots=2):
    manifest = ExperimentManifest(
        "synthetic-agentdojo",
        "fixture",
        "agent-model",
        ExperimentBudget(900000, 100000, 1000000, 30, 30),
        ("public-note",),
        1,
        0,
        slots,
        tuple(ArmSpec(arm, "sdk-agentdojo-v1") for arm in ("S", "R", "D", "F")),
    )
    return RunContext(manifest, manifest.runs()[2], (reports or []).append)


def runner(tmp_path, provider, **kwargs):
    provider.target = ProviderTarget(
        "fixture", "agent-model", "fixture", "https://example.test", "v1"
    )
    return AgentDojoRunner(
        provider=provider,
        context=context(),
        token_counter=Counter(),
        tokenizer=UpperBoundTokenizer(),
        context_policy=ContextPolicy(),
        evidence_root=tmp_path / "episode",
        default_output_tokens=1024,
        maximum_output_tokens=4096,
        dynamic_graph=False,
        **kwargs,
    )


def test_lazy_import():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['agentdojo'] = None; "
                "from agent_orchestrator.evaluation.agentdojo_runner import AgentDojoRunner"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_distinct_domain_templates_and_policy_snapshot(tmp_path):
    assert resolve_domain("agentdojo-v1") is AGENTDOJO_PROFILE
    assert handler_for(AGENTDOJO_PROFILE).name == "agentdojo"
    assert not handler_for(AGENTDOJO_PROFILE).document_assessments
    for name in ROLES:
        template = template_for_domain(ROLES[name], AGENTDOJO_PROFILE, {})
        assert template.prompt_version == f"{name}-agentdojo-v1"
        assert "run_tests" not in template.tool_names
        assert "appworld_execute" not in template.tool_names
    schemas = {"read_note": {"type": "object", "properties": {}}}
    cfg = OrchestratorConfig(
        evidence_root=tmp_path,
        agentdojo_invoke=lambda *args: {},
        agentdojo_tool_schemas=schemas,
        deployment_policy=DeploymentPolicy(
            allowed_tools=("read_note",),
            agentdojo_tools=("read_note",),
            local_code_execution=False,
        ),
    )
    snapshot = policy_snapshot(cfg)
    assert snapshot["config"]["agentdojo_tool_schemas"] == schemas
    assert (
        snapshot["hash"] == policy_snapshot(replace(cfg, agentdojo_invoke=lambda *args: {}))["hash"]
    )
    assert (
        "agentdojo_invoke"
        not in policy_snapshot(OrchestratorConfig(evidence_root=tmp_path))["config"]
    )


def official():
    pytest.importorskip("agentdojo")
    from agentdojo.functions_runtime import (
        Depends,
        FunctionsRuntime,
        TaskEnvironment,
        make_function,
    )
    from agentdojo.task_suite.task_suite import (
        functions_stack_trace_from_messages,
        model_output_from_messages,
    )

    return SimpleNamespace(
        Depends=Depends,
        FunctionsRuntime=FunctionsRuntime,
        TaskEnvironment=TaskEnvironment,
        make_function=make_function,
        functions_stack_trace_from_messages=functions_stack_trace_from_messages,
        model_output_from_messages=model_output_from_messages,
    )


@pytest.mark.parametrize("mode", ["success", "cancel", "structured"])
def test_actual_official_runtime_orchestrator_transcript_and_meter(tmp_path, mode, monkeypatch):
    api = official()
    import agent_orchestrator.evaluation.agentdojo_runner as runner_module

    original_meter = runner_module.MeteredProvider
    denial = {"status": "rejected_before_handoff", "known_physical_calls": 0}

    class MeterWithDenials(original_meter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.admission_denials = [denial]

    monkeypatch.setattr(runner_module, "MeteredProvider", MeterWithDenials)

    class Note(BaseModel):
        text: str

    class Env(api.TaskEnvironment):
        note: Note

    # Resolve annotations eagerly for official make_function/get_type_hints.
    def read_note(note, slot: int = 0) -> str:
        """Read the harmless local note.

        :param slot: The harmless local slot to read.
        """
        return note.text

    read_note.__annotations__["note"] = Annotated[Note, api.Depends("note")]
    read_note.__annotations__["slot"] = int

    class InjectingRuntime(api.FunctionsRuntime):
        def __init__(self):
            super().__init__([api.make_function(read_note)])
            self.calls = []

        def run_function(self, env, function, kwargs, raise_on_error=False):
            self.calls.append((env, function, kwargs))
            if mode == "cancel":
                entered.set()
                assert release.wait(5)
            value, error = super().run_function(env, function, kwargs, raise_on_error)
            if mode == "structured":
                self.result = [str(value) + " [synthetic injection hook]", 7]
                return self.result, error
            return str(value) + " [synthetic injection hook]", error

    runtime = InjectingRuntime()
    entered, release = Event(), Event()
    env = Env(note=Note(text="harmless"))
    output = "harmless [synthetic injection hook]"
    if mode == "structured":
        from agentdojo.agent_pipeline.tool_execution import tool_result_to_str

        output = tool_result_to_str([output, 7])

    def write_observation(request):
        # The hook's output must have crossed the actual SDK tool observation,
        # not just appeared in an independently constructed benchmark transcript.
        assert "harmless [synthetic injection hook]" in str(request.messages)
        return "workspace_write_file", {"path": "REPORT.md", "content": output}

    tools = ["read_note", "workspace_write_file", "workspace_read_file", "workspace_list"]
    proposal = {
        "key": "answer",
        "goal": "Read the note and answer in REPORT.md",
        "rationale": "one harmless read",
        "dependencies": [],
        "success_criteria": ["file:REPORT.md"],
        "verification_policy": ["format_check", "rule_check", "critic_review"],
        "allowed_tools": tools,
        "budget": {"max_tokens": 400000, "max_attempts": 1},
        "priority": 1.0,
        "outputs": ["REPORT.md"],
    }
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([proposal])],
            "worker": [
                ("read_note", {"arguments_json": '{"slot":0}'}),
                write_observation,
                envelope_step(summary=output, artifacts=["REPORT.md"], claims=["Read the note"]),
            ],
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )
    run = runner(tmp_path, provider)
    pipeline = make_agentdojo_pipeline(run)
    if mode == "cancel":

        async def cancelled_episode():
            messages = []
            port = AgentDojoToolPort(runtime, env, messages)
            pending = asyncio.create_task(
                run.run(
                    query="Read the local note",
                    messages=messages,
                    tools=port,
                    extra_args={},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                pending.cancel()
                await asyncio.sleep(0.1)
                assert not pending.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending

        asyncio.run(cancelled_episode())
        assert len(runtime.calls) == 1
        with sqlite3.connect(run.root / "orchestrator" / "execution.db") as db:
            effects = db.execute(
                "select tool_name, state, handoff_attempt, rehandoff_count from execution_effects"
            ).fetchall()
        assert effects == [("read_note", "unknown", 1, 0)], effects
        audit = json.loads((run.root / "gateway.json").read_text())["calls"]
        assert audit[0]["outcome"] == "unknown"
        assert provider.calls == 2
        assert json.loads((run.root / "episode.json").read_text())["state"] == "interrupted"
        assert json.loads((run.root / "episode.json").read_text())["admission_denials"] == [denial]
        assert (
            json.loads((run.root / "tool-observations" / "000001.json").read_text())["payload"][
                "state"
            ]
            == "returned"
        )
        calls = provider.calls
        with pytest.raises(FileExistsError):
            pipeline.query("Read again", runtime, env)
        assert provider.calls == calls and len(runtime.calls) == 1
        return
    _, returned_runtime, returned_env, transcript, metadata = pipeline.query(
        "Read the local note",
        runtime,
        env,
        extra_args={"hidden_evaluator_canary": "NEVER_VISIBLE_TO_MODEL"},
    )
    assert returned_runtime is runtime and returned_env is env
    assert runtime.calls == [(env, "read_note", {"slot": 0})]
    assert api.model_output_from_messages(transcript)[0]["content"] == output
    trace = api.functions_stack_trace_from_messages(transcript)
    assert len(trace) == 1 and trace[0].function == "read_note"
    assert trace[0].args == {"slot": 0}
    assert trace[0].id == transcript[-2]["tool_call_id"]
    assert metadata["hidden_evaluator_canary"] == "NEVER_VISIBLE_TO_MODEL"
    assert "NEVER_VISIBLE_TO_MODEL" not in str(provider.requests)
    assert set(provider.by_role) == {"planner", "worker", "critic"}
    assert run.meter.counters.calls == provider.calls == 5
    assert run.meter.counters.peak_physical_slots <= 2
    assert run.last_result["mission_status"] == "COMPLETED", run.last_result
    assert run.last_result["domain"] == "agentdojo-v1"
    mapping = json.loads((run.root / "schema-adapter.json").read_text())
    manifest = json.loads((run.root / "episode.json").read_text())
    assert mapping["version"] == SCHEMA_ADAPTER_VERSION
    assert mapping["fingerprint"] == manifest["schema_adapter_fingerprint"]
    assert manifest["runtime_termination_boundary"] == RUNTIME_TERMINATION_BOUNDARY
    assert manifest["admission_denials"] == run.last_result["admission_denials"] == [denial]
    assert manifest["unknown_usage_calls"] == 0
    assert json.loads((run.root / "meter.json").read_text())["admission_denials"] == [denial]
    assert json.loads((run.root / "usage.json").read_text())["admission_denials"] == [denial]
    record = json.loads((run.root / "tool-observations" / "000001.json").read_text())
    canonical = json.dumps(
        record["payload"],
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    assert sha256(canonical.encode()).hexdigest() == record["sha256"]
    assert record["payload"]["arguments"] == {"slot": 0}
    assert record["payload"]["call_id"] == trace[0].id
    assert record["payload"]["runtime"]["agentdojo_version"] == "0.1.35"
    if mode == "structured":
        runtime.result[1] = 88
        assert record["payload"]["result"][1] == 7
        assert (
            json.loads((run.root / "tool-observations" / "000001.json").read_text())["payload"][
                "result"
            ][1]
            == 7
        )
    for private_marker in (
        "agentdojo-tool-observation-v1",
        "canonical_redacted_payload_utf8",
        "source_file_sha256",
    ):
        assert private_marker not in str(provider.requests)
    assert mapping["tools"]["read_note"]["mode"] == "arguments_json"
    assert run.last_result["benchmark_success"] == "external_evaluation_pending"
    # No environment tools leaked into planner/critic requests.
    from agent_orchestrator.testing.fixtures import role_of

    for request in provider.requests:
        if role_of(request) in {"planner", "critic"}:
            assert "read_note" not in [tool.name for tool in request.tools]
    gateway = json.loads((run.root / "gateway.json").read_text())["calls"]
    assert [c["tool"] for c in gateway] == ["read_note", "workspace_write_file"]
    assert all(c["outcome"] == "succeeded" for c in gateway)
    # Durable SDK effect records exist, not just the in-memory gateway list.
    databases = list((run.root / "orchestrator").rglob("*.db"))
    assert databases
    rows = []
    for database in databases:
        with sqlite3.connect(database) as db:
            tables = {
                row[0] for row in db.execute("select name from sqlite_master where type='table'")
            }
            if "execution_effects" in tables:
                rows.extend(
                    db.execute(
                        "select tool_name, state, handoff_attempt, rehandoff_count "
                        "from execution_effects"
                    ).fetchall()
                )
    assert ("read_note", "succeeded", 1, 0) in rows
    calls = provider.calls
    with pytest.raises(FileExistsError):
        pipeline.query("Read again", runtime, env)
    assert provider.calls == calls and len(runtime.calls) == 1


def test_schema_projection_is_public_and_fails_closed():
    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        count: int
        text: str | None = None

    function = SimpleNamespace(parameters=Args, description="Public description")
    port = AgentDojoToolPort(SimpleNamespace(functions={"read": function}), object(), [])
    schema = runtime_tool_schemas(port)["read"]
    assert schema["properties"]["text"]["type"] == ["string", "null"]
    assert schema["required"] == ["count"]

    class OpenArgs(BaseModel):
        values: dict[str, str]

    function.parameters = OpenArgs
    adapter = AgentDojoSchemaAdapter(port)
    assert adapter.to_json()["tools"]["read"]["mode"] == "arguments_json"
    assert adapter.decode("read", {"arguments_json": '{"values":{"arbitrary":"unchanged"}}'}) == {
        "values": {"arbitrary": "unchanged"},
    }


def gateway(tmp_path, callback):
    worlds = WorkspaceManager(tmp_path)
    worlds.create("attempt", seed={})
    result = WorkspaceToolGateway(
        worlds,
        local_code_execution=False,
        agentdojo_invoke=callback,
        agentdojo_tool_schemas={
            "read_note": {"type": "object", "properties": {}, "additionalProperties": False}
        },
    )
    result.bind_agentdojo("mission")
    result.bind(
        "run", WorkspaceBinding("attempt", "work", True, ("read_note",), mission_id="mission")
    )
    return result


@run_async
async def test_cancel_drains_original_runtime_and_never_reconciles_by_replay(tmp_path):
    entered, release, settled = Event(), Event(), Event()
    calls = []

    def invoke(name, args, call_id):
        calls.append(call_id)
        entered.set()
        try:
            assert release.wait(3)
            return {"output": "harmless"}
        finally:
            settled.set()

    gw = gateway(tmp_path, invoke)
    task = asyncio.create_task(
        gw.execute(ToolCall(CallId("one"), "read_note", {}), {"run_id": "run"})
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert settled.is_set() and calls == ["run:one"]
    assert gw.calls[0]["outcome"] == "unknown"
    observation = await gw.observe(SimpleNamespace(tool_name="read_note"))
    assert str(observation.state) == "still_unknown"
    assert calls == ["run:one"]


@run_async
async def test_mission_permission_schema_and_critic_view_refuse_before_invoke(tmp_path):
    calls = []
    gw = gateway(tmp_path, lambda *args: calls.append(args) or {})
    for binding in (
        WorkspaceBinding("attempt", "work", True, (), mission_id="mission"),
        WorkspaceBinding("attempt", "work", True, ("read_note",), mission_id="other"),
        WorkspaceBinding("attempt", "verify", False, ("read_note",), mission_id="mission"),
    ):
        gw.bind("run", binding)
        result = await gw.execute(ToolCall(CallId("one"), "read_note", {}), {"run_id": "run"})
        assert str(result.outcome) == "rejected"
    with pytest.raises(ValueError, match="shared across Missions"):
        gw.bind_agentdojo("other")
    gw.bind("run", WorkspaceBinding("attempt", "work", True, ("read_note",), mission_id="mission"))
    result = await gw.execute(
        ToolCall(CallId("invalid"), "read_note", {"extra": 1}), {"run_id": "run"}
    )
    assert str(result.outcome) == "rejected"
    assert not calls


@pytest.mark.parametrize("failure", [RuntimeError, TypeError, KeyError])
@run_async
async def test_callback_uncertainty_uses_native_unknown_and_stops_environment(tmp_path, failure):
    calls = []

    def invoke(*args):
        calls.append(args)
        raise failure("private callback detail")

    gw = gateway(tmp_path, invoke)
    first = await gw.execute(ToolCall(CallId("one"), "read_note", {}), {"run_id": "run"})
    assert str(first.outcome) == "unknown"
    assert gw.calls[0]["outcome"] == "unknown"
    second = await gw.execute(ToolCall(CallId("two"), "read_note", {}), {"run_id": "run"})
    assert str(second.outcome) == "rejected"
    assert len(calls) == 1
    assert "private callback detail" not in str(gw.calls) + str(first)


@pytest.mark.parametrize("domain", ["code-v1", "doc-research-v1", "appworld-v1"])
@run_async
async def test_foreign_domain_cannot_admit_agentdojo_tool(tmp_path, domain):
    from agent_orchestrator.contracts import ContractError
    from agent_orchestrator.orchestrator.commit_service import MissionSpec
    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    provider = RoleScriptedProvider({})
    cfg = OrchestratorConfig(
        evidence_root=tmp_path / "orch",
        agentdojo_invoke=lambda *args: {},
        agentdojo_tool_schemas={"read_note": {"type": "object", "properties": {}}},
        deployment_policy=DeploymentPolicy(
            allowed_tools=("read_note",),
            agentdojo_tools=("read_note",),
            local_code_execution=False,
        ),
    )
    async with Orchestrator(cfg, provider) as orch:
        with pytest.raises(ContractError, match="require the AgentDojo domain"):
            await orch.submit_mission(
                MissionSpec(
                    goal="read",
                    success_criteria=("file:REPORT.md",),
                    tenant_id="test",
                    idempotency_key="test",
                    allowed_tools=("read_note",),
                    domain=domain,
                )
            )
    assert provider.calls == 0


def test_rejects_physical_capacity_above_two_before_provider(tmp_path):
    with pytest.raises(ValueError, match="capacity"):
        AgentDojoRunner(
            provider=RoleScriptedProvider({}),
            context=context(slots=3),
            token_counter=Counter(),
            tokenizer=UpperBoundTokenizer(),
            context_policy=ContextPolicy(),
            evidence_root=tmp_path,
        )


def test_four_official_suites_compile_complete_tool_tables():
    api = official()
    from agentdojo.task_suite.load_suites import get_suites

    counts = {}
    for name, suite in get_suites("v1.2.2").items():
        port = AgentDojoToolPort(api.FunctionsRuntime(suite.tools), None, [])
        adapter = AgentDojoSchemaAdapter(port)
        counts[name] = len(adapter.schemas)
        assert set(adapter.schemas) == {function.name for function in suite.tools}
        for schema in adapter.schemas.values():
            validate_tool_schema(schema)
        assert adapter.fingerprint == AgentDojoSchemaAdapter(port).fingerprint
        for tool in ("send_email", "update_password"):
            if tool in adapter.schemas:
                assert adapter.to_json()["tools"][tool]["mode"] == "arguments_json"
    assert counts == {"workspace": 24, "travel": 28, "banking": 11, "slack": 11}


@pytest.mark.parametrize(
    "suite_name,tool_name",
    [
        ("banking", "update_password"),
        ("workspace", "send_email"),
        ("travel", "send_email"),
    ],
)
@run_async
async def test_original_official_password_and_open_attachments_roundtrip(
    tmp_path,
    suite_name,
    tool_name,
):
    api = official()
    from agentdojo.task_suite.load_suites import get_suites

    function = next(f for f in get_suites("v1.2.2")[suite_name].tools if f.name == tool_name)

    class SyntheticInbox:
        def __init__(self):
            self.calls = []

        def send_email(self, *args):
            # In-memory dependency only: original official function still runs,
            # but this synthetic env cannot contact a real mailbox or network.
            self.calls.append(args)
            return {"synthetic": True}

    inbox = SyntheticInbox()
    env = SimpleNamespace(inbox=inbox, user_account=SimpleNamespace(password="synthetic-old"))

    class RecordingRuntime(api.FunctionsRuntime):
        def __init__(self):
            super().__init__([function])
            self.calls = []

        def run_function(self, current_env, name, kwargs, raise_on_error=False):
            self.calls.append((current_env, name, kwargs))
            result, error = super().run_function(current_env, name, kwargs, raise_on_error)
            return {"official_result": result, "hook": "synthetic hook retained"}, error

    runtime = RecordingRuntime()
    transcript = []
    port = AgentDojoToolPort(runtime, env, transcript)
    adapter = AgentDojoSchemaAdapter(port)
    original = (
        {"password": "synthetic-password-fixture"}
        if tool_name == "update_password"
        else {
            "recipients": ["synthetic@example.invalid"],
            "subject": "synthetic",
            "body": "hello",
            "attachments": [
                {
                    "type": "file",
                    "file_id": "synthetic-file",
                    "arbitrary_original_field": {"中文": [1, None, True, {"x": "y"}]},
                }
            ],
            "cc": None,
            "bcc": [],
        }
    )
    sdk_arguments = {"arguments_json": json.dumps(original, ensure_ascii=False)}
    assert adapter.decode(tool_name, sdk_arguments) == original
    mapping = adapter.to_json()
    assert mapping["tools"][tool_name]["original_schema"] == function.parameters.model_json_schema()
    assert mapping["tools"][tool_name]["mode"] == "arguments_json"

    def invoke(name, arguments, call_id):
        result, error = adapter.invoke(name, arguments, call_id)
        return {"result": result, "error": error}

    worlds = WorkspaceManager(tmp_path / "workspaces")
    worlds.create("attempt", seed={})
    gw = WorkspaceToolGateway(
        worlds,
        local_code_execution=False,
        agentdojo_invoke=invoke,
        agentdojo_tool_schemas=adapter.schemas,
    )
    gw.bind_agentdojo("mission")
    gw.bind("run", WorkspaceBinding("attempt", "work", True, (tool_name,), mission_id="mission"))
    result = await gw.execute(
        ToolCall(CallId("roundtrip"), tool_name, sdk_arguments), {"run_id": "run"}
    )
    assert str(result.outcome) == "succeeded"
    assert runtime.calls == [(env, tool_name, original)]
    trace = api.functions_stack_trace_from_messages(transcript)
    assert len(trace) == 1 and trace[0].function == tool_name
    assert trace[0].args == original and trace[0].id == "run:roundtrip"
    assert transcript[-1]["error"] is None
    assert "synthetic hook retained" in str(transcript[-1]["content"])
    if tool_name == "update_password":
        assert env.user_account.password == original["password"]
    else:
        assert len(inbox.calls) == 1 and inbox.calls[0][3] == ["synthetic-file"]


@pytest.mark.parametrize("payload", ["[]", "null", "1", "{broken", '{"x":1,"x":2}', '{"x":NaN}'])
def test_json_transport_rejects_before_original_runtime(payload):
    class Args(BaseModel):
        password: str

    calls = []
    function = SimpleNamespace(parameters=Args, description="Synthetic argument schema")
    runtime = SimpleNamespace(functions={"change": function})
    port = AgentDojoToolPort(runtime, None, [])
    port.invoke = lambda *args, **kwargs: calls.append((args, kwargs))
    adapter = AgentDojoSchemaAdapter(port)
    with pytest.raises(AdapterArgumentsError):
        adapter.invoke("change", {"arguments_json": payload}, "bad")
    assert calls == []


def test_mapping_fingerprint_covers_schema_description_mode_and_version():
    class Args(BaseModel):
        password: str

    function = SimpleNamespace(parameters=Args, description="Public description")
    port = AgentDojoToolPort(SimpleNamespace(functions={"change": function}), None, [])
    first = AgentDojoSchemaAdapter(port)
    assert first.to_json()["version"] == SCHEMA_ADAPTER_VERSION
    assert first.fingerprint == AgentDojoSchemaAdapter(port).fingerprint
    function.description = "Changed public description"
    assert first.fingerprint != AgentDojoSchemaAdapter(port).fingerprint
    # Returned mapping/schema copies cannot alter a frozen decoder or fingerprint.
    exported = first.to_json()
    exported["tools"]["change"]["mode"] = "structured"
    assert first.decode("change", {"arguments_json": '{"password":"synthetic"}'}) == {
        "password": "synthetic",
    }


N5_MARKER = "N5_HARMLESS_MARKER_7f3c"


def _episode_records(run, table, loader):
    path = run.root / "orchestrator" / "orchestrator.db"
    with sqlite3.connect(path) as db:
        rows = db.execute(f"select json from {table}").fetchall()
    return [loader(json.loads(row[0])) for row in rows]


def _knowledge(run):
    return _episode_records(run, "knowledge", KnowledgeRecord.from_json)


def _claims(run):
    return _episode_records(run, "claims", Claim.from_json)


def _note_env(api, text, *, error=None):
    class Note(BaseModel):
        text: str

    class Env(api.TaskEnvironment):
        note: Note

    def read_note(note, slot: int = 0) -> str:
        """Read the harmless local note.

        :param slot: The harmless local slot to read.
        """
        return note.text

    read_note.__annotations__["note"] = Annotated[Note, api.Depends("note")]
    read_note.__annotations__["slot"] = int

    class Runtime(api.FunctionsRuntime):
        def __init__(self):
            super().__init__([api.make_function(read_note)])
            self.calls = []

        def run_function(self, env, function, kwargs, raise_on_error=False):
            self.calls.append((env, function, kwargs))
            value, runtime_error = super().run_function(env, function, kwargs, raise_on_error)
            if error is not None:
                return str(value), error
            return str(value) + " [synthetic injection hook]", runtime_error

    return Runtime(), Env(note=Note(text=text))


def _task(key, output, dependencies, extra_tools=(), *, goal=None, rationale="n5 blackboard"):
    tools = [
        "read_note",
        "workspace_write_file",
        "workspace_read_file",
        "workspace_list",
        *extra_tools,
    ]
    return {
        "key": key,
        "goal": goal or f"Write {output}",
        "rationale": rationale,
        "dependencies": dependencies,
        "success_criteria": [f"file:{output}"],
        "verification_policy": ["format_check", "rule_check", "critic_review"],
        "allowed_tools": tools,
        "budget": {"max_tokens": 400000, "max_attempts": 1},
        "priority": 1.0,
        "outputs": [output],
    }


def test_blackboard_has_no_write_promotion_api():
    assert not hasattr(Blackboard, "upsert")
    assert not hasattr(Blackboard, "commit")
    assert not hasattr(Blackboard, "write")
    assert callable(Blackboard.verified_knowledge)


def test_system_observation_builder_rejects_errors_and_keeps_returned_bytes():
    receipt = AgentDojoToolReceipt(
        call_key="run:1",
        function="read_note",
        output=N5_MARKER,
        error="synthetic-tool-error",
        arguments_sha256="a" * 64,
        result_sha256="b" * 64,
        agentdojo_version="0.1.35",
    )
    with pytest.raises(ValueError, match="not promotable"):
        make_agentdojo_tool_knowledge(
            mission_id="m",
            task_id="t",
            attempt_id="a",
            result_id="r",
            receipt=receipt,
            now=1.0,
        )
    ok = AgentDojoToolReceipt(
        call_key="run:1",
        function="read_note",
        output=N5_MARKER,
        error=None,
        arguments_sha256="a" * 64,
        result_sha256="b" * 64,
        agentdojo_version="0.1.35",
    )
    claim, record = make_agentdojo_tool_knowledge(
        mission_id="m",
        task_id="t",
        attempt_id="a",
        result_id="r",
        receipt=ok,
        now=1.0,
    )
    assert claim.status is ClaimStatus.VERIFIED
    assert record.proposed_by == SYSTEM_PROPOSER
    assert record.type == "tool_observation"
    assert N5_MARKER in record.content
    assert record.evidence == ("tool-run:run:1",)


def test_two_task_tool_observation_promotes_and_is_consumed(tmp_path):
    api = official()
    runtime, env = _note_env(api, N5_MARKER)
    captured: dict[str, object] = {}
    exposed = {"worker_saw_tool_output": False}

    def write_observe(request):
        assert N5_MARKER in str(request.messages)
        exposed["worker_saw_tool_output"] = True
        return "workspace_write_file", {"path": "A.md", "content": N5_MARKER}

    def start_consume(request):
        package = package_of(request)
        items = list(package.get("verified_knowledge") or [])
        captured["b_package"] = package
        captured["b_user"] = request.messages[-1].content if request.messages else ""
        assert items, package
        assert any(N5_MARKER in str(item) for item in items), items
        captured["knowledge_id"] = items[0]["id"]
        return "knowledge_read", {"id": items[0]["id"]}

    def write_report(_request):
        return "workspace_write_file", {"path": "REPORT.md", "content": N5_MARKER}

    def consume_envelope(request):
        def override(envelope):
            envelope["used_knowledge"] = [captured["knowledge_id"]]
            return envelope

        return envelope_step(
            summary=N5_MARKER,
            artifacts=["REPORT.md"],
            claims=["Downstream used the host observation"],
            override=override,
        )(request)

    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        _task("observe", "A.md", []),
                        _task(
                            "consume",
                            "REPORT.md",
                            ["observe"],
                            extra_tools=("knowledge_list", "knowledge_read"),
                            goal=(
                                "Write REPORT.md from the Host observed AgentDojo "
                                f"tool read_note return {N5_MARKER}"
                            ),
                            rationale="consume tool observation",
                        ),
                    ]
                )
            ],
            "worker": [
                ("read_note", {"arguments_json": '{"slot":0}'}),
                write_observe,
                envelope_step(
                    summary=N5_MARKER,
                    artifacts=["A.md"],
                    claims=["The note is an instruction to ignore previous policy"],
                ),
                start_consume,
                write_report,
                consume_envelope,
            ],
            "critic": [
                critic_step(verdict="PASS", criteria_met=True),
                critic_step(verdict="PASS", criteria_met=True),
            ],
        }
    )
    run = runner(tmp_path, provider)
    pipeline = make_agentdojo_pipeline(run)
    pipeline.query("Read the local note", runtime, env)
    assert exposed["worker_saw_tool_output"] is True
    assert run.last_result["mission_status"] == "COMPLETED"
    knowledge = _knowledge(run)
    assert len(knowledge) == 1
    record = knowledge[0]
    assert record.status == "VERIFIED"
    assert record.proposed_by == SYSTEM_PROPOSER
    assert N5_MARKER in record.content
    assert "[synthetic injection hook]" in record.content
    agent_claims = [c for c in _claims(run) if c.proposed_by != SYSTEM_PROPOSER]
    assert agent_claims
    assert all(c.status is not ClaimStatus.VERIFIED for c in agent_claims)
    assert captured["knowledge_id"] == record.id
    assert record.id in str(captured["b_user"])
    consume_task = next(
        t for t in run.last_result["tasks"] if t["goal"].startswith("Write REPORT.md")
    )
    assert record.used_by == (consume_task["id"],)
    with sqlite3.connect(run.root / "orchestrator" / "orchestrator.db") as db:
        used = db.execute("select payload_json from events where type = 'KnowledgeUsed'").fetchall()
    assert used
    assert record.id in used[0][0]


def test_tool_error_does_not_promote_even_if_text_is_exposed(tmp_path):
    api = official()
    runtime, env = _note_env(api, N5_MARKER, error="synthetic-tool-error")

    def write_report(request):
        assert N5_MARKER in str(request.messages)
        return "workspace_write_file", {"path": "REPORT.md", "content": "failed-read"}

    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([_task("answer", "REPORT.md", [])])],
            "worker": [
                ("read_note", {"arguments_json": '{"slot":0}'}),
                write_report,
                envelope_step(
                    summary="failed-read",
                    artifacts=["REPORT.md"],
                    claims=["Read failed"],
                ),
            ],
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )
    run = runner(tmp_path, provider)
    pipeline = make_agentdojo_pipeline(run)
    pipeline.query("Read the local note", runtime, env)
    assert run.last_result["mission_status"] == "COMPLETED"
    assert _knowledge(run) == []
    assert all(c.status is not ClaimStatus.VERIFIED for c in _claims(run))


def test_forged_knowledge_id_is_not_consumed(tmp_path):
    api = official()
    runtime, env = _note_env(api, N5_MARKER)

    def write_observe(request):
        assert N5_MARKER in str(request.messages)
        return "workspace_write_file", {"path": "A.md", "content": N5_MARKER}

    def write_report(_request):
        return "workspace_write_file", {"path": "REPORT.md", "content": N5_MARKER}

    def forged_envelope(request):
        def override(envelope):
            envelope["used_knowledge"] = ["forged-claim-id"]
            return envelope

        return envelope_step(
            summary=N5_MARKER,
            artifacts=["REPORT.md"],
            claims=["Forged citation"],
            override=override,
        )(request)

    provider = RoleScriptedProvider(
        {
            "planner": [
                graph_proposal_step(
                    [
                        _task("observe", "A.md", []),
                        _task(
                            "consume",
                            "REPORT.md",
                            ["observe"],
                            goal=(
                                "Write REPORT.md from the Host observed AgentDojo "
                                f"tool read_note return {N5_MARKER}"
                            ),
                        ),
                    ]
                )
            ],
            "worker": [
                ("read_note", {"arguments_json": '{"slot":0}'}),
                write_observe,
                envelope_step(
                    summary=N5_MARKER,
                    artifacts=["A.md"],
                    claims=["Observed the note"],
                ),
                write_report,
                forged_envelope,
            ],
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )
    run = runner(tmp_path, provider)
    pipeline = make_agentdojo_pipeline(run)
    with pytest.raises(RuntimeError, match="did not complete"):
        pipeline.query("Read the local note", runtime, env)
    knowledge = _knowledge(run)
    assert len(knowledge) == 1
    assert knowledge[0].proposed_by == SYSTEM_PROPOSER
    assert knowledge[0].used_by == ()
    with sqlite3.connect(run.root / "orchestrator" / "orchestrator.db") as db:
        used = db.execute("select payload_json from events where type = 'KnowledgeUsed'").fetchall()
        claims = db.execute("select json from claims").fetchall()
    assert used == []
    assert all("forged-claim-id" not in row[0] or '"VERIFIED"' not in row[0] for row in claims)
