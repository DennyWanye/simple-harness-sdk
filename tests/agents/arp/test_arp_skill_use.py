"""Skill load / execute and the model-side catalogue tools (§9.8–9.10): load binds a
SkillUse and the instruction files reach the model once, in section E of the next
request, bound to the load receipt in the manifest; execute returns INSTRUCTIONS text,
routes SCRIPT through the approved runner port with a fixed argv and schema-checked
input/output, refuses WORKFLOW by name, and never widens the session's tool exposure."""

from __future__ import annotations

import asyncio
import json

from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import AcceptingAssurance, ScriptedRunner, admit_skill, import_skill, md_bundle, native_bundle, receipt, trial_command

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp import store
from simple_harness.agents.arp.context.skill_blocks import replay_skill_blocks, skill_message
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.skill_tools import SKILL_DISCOVER_TOOL_NAME, SKILL_EXECUTE_TOOL_NAME, SKILL_LOAD_TOOL_NAME, TOOL_DISCOVER_TOOL_NAME
from simple_harness.agents.arp.strict import digest, plain
from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.tools import CancellationToken, ToolContext

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
SCRIPT = b"import json,sys\nprint(json.dumps({'ok': True}))\n"
IO_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _tools(runtime):  # type: ignore[no-untyped-def]
    return {t.spec.name: t for t in runtime._session_tools.function_tools()}


def _context(agent, n: int) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(RunId(agent.run_id), RequestId(f"{agent.run_id}:tool:{n}"), CancellationToken(), call_id=CallId(f"call-{n}"))


async def _agent(runtime, *, turns: int = 1):  # type: ignore[no-untyped-def]
    agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
    for i in range(turns):
        r = await agent.submit(f"第 {i} 轮。", input_id=f"i{i}")
        await agent.wait_turn(r.turn_id, timeout=10)
    return agent


def _session(runtime, agent):  # type: ignore[no-untyped-def]
    return store.read_live_session(runtime.uow.database.connection, agent.run_id)


def _admit_schema(runtime, entry_id: str) -> Pin:  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    revision, _ = service.register("SCHEMA", IO_SCHEMA, entry_id=entry_id, caller=trusted_caller(), command_id=f"schema:{entry_id}")
    for state in ("TRIAL", "ADMITTED"):
        service.transition(revision.pin, state=state, caller=trusted_caller(), command_id=f"schema:{entry_id}:{state}")
    return revision.pin


def _script_bundle(runtime, *, skill_id: str = "runner-skill", argv=None, required_tool_refs=None) -> bytes:  # type: ignore[no-untyped-def]
    report = runtime.arp.bootstrap
    schema = _admit_schema(runtime, f"io.{skill_id}")
    return native_bundle(
        runtime, skill_id=skill_id,
        implementation={"kind": "SCRIPT", "script_path": "scripts/run.py", "runner_ref": report.provider_ref.to_json(), "argv_template": argv or ["python3", "scripts/run.py", "{input_json}", "{output_json}"]},
        files={"SKILL.md": (b"# runner\n", "INSTRUCTIONS"), "scripts/run.py": (SCRIPT, "SCRIPT")},
        input_schema=schema, output_schema=schema, capability=report.capability_ref, required_tool_refs=required_tool_refs,
    )


def test_load_binds_a_skill_use_and_exposes_each_instruction_file_once_in_section_e(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["好的。"] * 5)
        runtime = build(tmp_path, provider, acceptance=AcceptingAssurance())
        async with runtime:
            revision = import_skill(runtime, md_bundle("loaded", "记住口令 青鸟。"), command="i1").revision
            admit_skill(runtime, revision, command="c1")
            agent = await _agent(runtime)
            tools = _tools(runtime)
            result = await tools[SKILL_LOAD_TOOL_NAME].invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 1))
            assert result.outcome.value == "succeeded", result
            use = plain(result.value)
            session = _session(runtime, agent)
            assert use["mode"] == "INSTRUCTIONS" and use["session_id"] == session.session_id and use["skill_ref"] == revision.pin.to_json()
            assert use["dependency_lock_refs"][0]["kind"] == "dependency_lock" and use["evaluation_ref"]["kind"] == "evaluation" and use["reservation_fact_ref"] is None
            assert store.read_skill_use(runtime.uow.database.connection, use["use_id"]) == use
            # The same call re-sent replays the same use; a second load of the same skill is another use.
            again = await tools[SKILL_LOAD_TOOL_NAME].invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 1))
            assert plain(again.value) == use
            other = await tools[SKILL_LOAD_TOOL_NAME].invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 2))
            assert plain(other.value)["use_id"] != use["use_id"]
            # Nothing reached the model yet; the next request carries each file once.
            assert not any("skill_instructions" in str(m.content) for m in provider.requests[-1].messages)
            r = await agent.submit("继续。", input_id="i1")
            await agent.wait_turn(r.turn_id, timeout=10)
            wire = provider.requests[-1].messages
            blocks = [m for m in wire if "<skill_instructions" in str(m.content)]
            # Only the instruction file is loaded (reference files go through details/artifact reads).
            assert len(blocks) == 1 and 'skill="loaded@1" path="SKILL.md"' in str(blocks[0].content) and "青鸟" in str(blocks[0].content)
            assert wire.index(blocks[0]) == 1  # right after the control instructions
            manifest = store.latest_context(runtime.uow.database.connection, session.session_id).manifest
            e_blocks = [s for s in manifest["sections"] if s["section"] == "E" and s["block_id"].startswith("skill:")]
            assert [s["block_id"] for s in e_blocks] == ["skill:loaded@1:SKILL.md"]
            assert all(s["trust"] == "SKILL_INSTRUCTIONS" and s["required"] and s["budget_charge"] > 0 for s in e_blocks)
            assert e_blocks[0]["source_refs"][0]["kind"] == "receipt" and e_blocks[0]["source_refs"][0]["id"] == f"skill_load:{use['use_id']}"
            assert manifest["skill_refs"] == [revision.pin.to_json()]
            # A frozen manifest replays its own blocks byte for byte.
            replayed = replay_skill_blocks(runtime.arp, manifest, count=runtime.arp.index.count)
            assert [skill_message(b).content for b in replayed] == [str(m.content) for m in blocks]
            # A later turn keeps the blocks (still one copy each), and the budget counts them.
            r = await agent.submit("再来。", input_id="i2")
            await agent.wait_turn(r.turn_id, timeout=10)
            assert sum("<skill_instructions" in str(m.content) for m in provider.requests[-1].messages) == 1
            # Suspending the skill stops any further exposure of what was loaded (§9.7).
            runtime.arp.lifecycle.suspend({"schema_version": 1, "skill_ref": revision.pin.to_json(), "reason": "停"}, caller=trusted_caller(), command_id="s1")
            r = await agent.submit("还有。", input_id="i3")
            await agent.wait_turn(r.turn_id, timeout=10)
            assert not any("<skill_instructions" in str(m.content) for m in provider.requests[-1].messages)
            assert store.latest_context(runtime.uow.database.connection, session.session_id).manifest["skill_refs"] == []

    _run(case())


def test_load_is_refused_before_it_could_exhaust_the_context_budget(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"] * 3), acceptance=AcceptingAssurance(), input_limit=4096)
        async with runtime:
            small = import_skill(runtime, md_bundle("small", "短说明。"), command="i1").revision
            admit_skill(runtime, small, command="c1")
            huge = import_skill(runtime, md_bundle("huge", " ".join(f"词{i}" for i in range(6000))), command="i2").revision
            admit_skill(runtime, huge, command="c2")
            agent = await _agent(runtime)
            load = _tools(runtime)[SKILL_LOAD_TOOL_NAME]
            assert (await load.invoke({"skill_ref": small.pin.to_json()}, _context(agent, 1))).outcome.value == "succeeded"
            refused = await load.invoke({"skill_ref": huge.pin.to_json()}, _context(agent, 2))
            assert refused.outcome.value == "failed" and refused.error_code == "skill_required_context_too_large"
            session = _session(runtime, agent)
            assert [u["skill_ref"]["id"] for u in store.read_skill_uses(runtime.uow.database.connection, session.session_id)] == ["small"]
            # The Session keeps working: the next prepare succeeds with the small block only.
            r = await agent.submit("继续。", input_id="i1")
            await agent.wait_turn(r.turn_id, timeout=10)
            assert store.latest_context(runtime.uow.database.connection, session.session_id).manifest["skill_refs"] == [small.pin.to_json()]

    _run(case())


def test_load_and_execute_refuse_skills_that_are_not_admitted_and_usable(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"] * 2), acceptance=AcceptingAssurance())
        async with runtime:
            lifecycle = runtime.arp.lifecycle
            revision = import_skill(runtime, md_bundle("gated"), command="i1").revision
            agent = await _agent(runtime)
            tools = _tools(runtime)
            load = tools[SKILL_LOAD_TOOL_NAME]
            quarantined = await load.invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 1))
            assert quarantined.outcome.value == "failed" and quarantined.error_code == "skill_skill_trial_required"
            binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t1")
            trial = await load.invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 2))
            assert trial.error_code == "skill_skill_not_admitted"
            from skill_fixture import admit_command

            lifecycle.admit(admit_command(runtime, revision, binding), caller=trusted_caller(), command_id="a1")
            assert (await load.invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 3))).outcome.value == "succeeded"
            lifecycle.suspend({"schema_version": 1, "skill_ref": revision.pin.to_json(), "reason": "停"}, caller=trusted_caller(), command_id="s1")
            suspended = await load.invoke({"skill_ref": revision.pin.to_json()}, _context(agent, 4))
            assert suspended.error_code == "skill_skill_not_admitted"
            executed = await tools[SKILL_EXECUTE_TOOL_NAME].invoke({"skill_ref": revision.pin.to_json(), "arguments": {}}, _context(agent, 5))
            assert executed.error_code == "skill_skill_not_admitted"
            # Unknown pins and non-skill pins are named too.
            ghost = await load.invoke({"skill_ref": Pin("skill", "ghost", 1, "0" * 64).to_json()}, _context(agent, 6))
            assert ghost.outcome.value == "failed" and ghost.error_code.startswith("skill_")

    _run(case())


def test_execute_returns_instructions_inline_and_discover_pages_the_model_view(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"]), acceptance=AcceptingAssurance())
        async with runtime:
            revision = import_skill(runtime, md_bundle("inline", "正文在此。"), command="i1").revision
            admit_skill(runtime, revision, command="c1")
            agent = await _agent(runtime)
            tools = _tools(runtime)
            result = await tools[SKILL_EXECUTE_TOOL_NAME].invoke({"skill_ref": revision.pin.to_json(), "arguments": {}}, _context(agent, 1))
            assert result.outcome.value == "succeeded", result
            view = plain(result.value)
            assert view["status"] == "SUCCEEDED" and view["representation"] == "INLINE" and "正文在此" in view["inline_text"]
            assert view["tool_ref"]["id"] == "skill_execute" and view["call_ref"]["kind"] == "invocation" and view["operation_intent_ref"] is None
            session = _session(runtime, agent)
            uses = store.read_skill_uses(runtime.uow.database.connection, session.session_id)
            assert len(uses) == 1 and uses[0]["mode"] == "INSTRUCTIONS"
            executed = store.read_original_receipt(runtime.uow.database.connection, kind="skill_execute", receipt_key=f"{uses[0]['use_id']}:{view['call_ref']['id']}")
            assert executed["view"] == view and executed["runner_receipt_ref"] is None
            # An executed INSTRUCTIONS skill is not loaded into E: no load receipt, no block.
            assert store.read_original_receipt(runtime.uow.database.connection, kind="skill_load", receipt_key=uses[0]["use_id"]) is None
            # Discovery: MODEL view pages, namespace forced by the session, no secrets.
            skills = plain((await tools[SKILL_DISCOVER_TOOL_NAME].invoke({"limit": 8}, _context(agent, 2))).value)
            assert skills["access_view"] == "MODEL" and [i["name"] for i in skills["items"]] == ["inline"] and skills["items"][0]["current_usable"] is True
            page = plain((await tools[TOOL_DISCOVER_TOOL_NAME].invoke({"limit": 3}, _context(agent, 3))).value)
            assert page["has_more"] and len(page["items"]) == 3 and all(i["kind"] == "TOOL" for i in page["items"])
            rest = plain((await tools[TOOL_DISCOVER_TOOL_NAME].invoke({"limit": 64, "cursor": page["next_cursor"]}, _context(agent, 4))).value)
            names = {i["name"] for i in page["items"] + rest["items"]}
            assert {"skill_load", "skill_execute", "skill_discover", "tool_discover", "session_history_search"} <= names
            assert not any("credential" in json.dumps(i) or "endpoint" in json.dumps(i) for i in rest["items"])

    _run(case())


def test_workflow_skills_are_refused_by_name(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"]), acceptance=AcceptingAssurance())
        async with runtime:
            data = native_bundle(
                runtime, skill_id="flow", implementation={"kind": "WORKFLOW", "workflow_ref": Pin("workflow", "wf-1", 1, digest("wf")).to_json()},
                files={"SKILL.md": (b"# flow\n", "INSTRUCTIONS")},
            )
            revision = import_skill(runtime, data, command="i1", fmt="NATIVE").revision
            admit_skill(runtime, revision, command="c1")
            agent = await _agent(runtime)
            result = await _tools(runtime)[SKILL_EXECUTE_TOOL_NAME].invoke({"skill_ref": revision.pin.to_json(), "arguments": {}}, _context(agent, 1))
            assert result.outcome.value == "failed" and result.error_code == "skill_workflow_unavailable"
            assert store.read_skill_uses(runtime.uow.database.connection, _session(runtime, agent).session_id) == ()

    _run(case())


def test_script_skills_run_only_through_the_approved_runner_with_a_fixed_argv(tmp_path) -> None:
    async def case() -> None:
        runner = ScriptedRunner([
            receipt(output=b'{"n": 2}'),
            receipt(output=b"not json"),
            receipt(exit_code=3, output=None),
            receipt(terminal="UNKNOWN", exit_code=None, output=None),
            receipt(output=b'{"n": "x"}'),
            receipt(output=b'{"n": 1}', truncated=True),
        ])
        runtime = build(tmp_path, ScriptedProvider(["好的。"]), acceptance=AcceptingAssurance(), script_runner=runner)
        async with runtime:
            revision = import_skill(runtime, _script_bundle(runtime), command="i1", fmt="NATIVE").revision
            admit_skill(runtime, revision, command="c1")
            agent = await _agent(runtime)
            execute = _tools(runtime)[SKILL_EXECUTE_TOOL_NAME]
            ok = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 1))
            assert ok.outcome.value == "succeeded", ok
            view = plain(ok.value)
            assert view["status"] == "SUCCEEDED" and json.loads(view["inline_text"]) == {"n": 2} and view["source_hash"]
            run = runner.runs[0]
            assert run.argv == ("python3", "scripts/run.py", "{input_json}", "{output_json}") and run.input_json == b'{"n":1}' and run.script_bytes == SCRIPT
            assert run.runner_ref == runtime.arp.bootstrap.provider_ref and run.workspace_key.startswith("skill/") and run.timeout_ms > 0
            session = _session(runtime, agent)
            uses = store.read_skill_uses(runtime.uow.database.connection, session.session_id)
            assert [u["mode"] for u in uses] == ["SCRIPT"]
            recorded = store.read_original_receipt(runtime.uow.database.connection, kind="skill_execute", receipt_key=f"{uses[0]['use_id']}:{view['call_ref']['id']}")
            assert recorded["runner_receipt_ref"]["kind"] == "receipt" and recorded["view"] == view
            # exit 0 + invalid JSON, non-zero exit, unknown end, schema-invalid output, truncated output.
            bad_json = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 2))
            assert bad_json.outcome.value == "failed" and bad_json.error_code == "skill_skill_output_invalid"
            exit3 = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 3))
            assert exit3.error_code == "skill_skill_output_invalid" and "exited with 3" in str(exit3.public_message)
            unknown = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 4))
            assert unknown.outcome.value == "unknown" and plain(unknown.value)["error_code"] == "UNKNOWN_REQUIRES_RECONCILIATION"
            wrong_type = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 5))
            assert wrong_type.error_code == "skill_skill_output_invalid"
            truncated = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 6))
            assert truncated.error_code == "skill_skill_output_invalid"
            # Input is checked against the skill's input schema before any run.
            bad_input = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": "one"}}, _context(agent, 7))
            assert bad_input.outcome.value == "failed" and len(runner.runs) == 6

    _run(case())


def test_script_execution_refuses_missing_runner_partial_tokens_and_unexposed_tools(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["好的。"]), acceptance=AcceptingAssurance())  # no runner port
        async with runtime:
            service = runtime.arp.catalogue
            plain_skill = import_skill(runtime, _script_bundle(runtime, skill_id="no-runner"), command="i1", fmt="NATIVE").revision
            admit_skill(runtime, plain_skill, command="c1")
            partial = import_skill(runtime, _script_bundle(runtime, skill_id="partial", argv=["python3", "--in={input_json}", "{output_json}"]), command="i2", fmt="NATIVE").revision
            admit_skill(runtime, partial, command="c2")
            # A tool that is admitted in the catalogue but not exposed to this session.
            report = runtime.arp.bootstrap
            sample = cat.latest_revision(service.connection, service.namespace_id, "TOOL", "session_history_read")
            phantom, _ = service.register("TOOL", {**dict(sample.body), "tool_id": "phantom_tool", "implementation_digest": digest("phantom")}, entry_id="phantom_tool", caller=trusted_caller(), command_id="phantom")
            for state in ("TRIAL", "ADMITTED"):
                service.transition(phantom.pin, state=state, caller=trusted_caller(), command_id=f"phantom:{state}")
            needy = import_skill(runtime, _script_bundle(runtime, skill_id="needy", required_tool_refs=[phantom.pin.to_json()]), command="i3", fmt="NATIVE").revision
            admit_skill(runtime, needy, command="c3")
            agent = await _agent(runtime)
            execute = _tools(runtime)[SKILL_EXECUTE_TOOL_NAME]
            no_runner = await execute.invoke({"skill_ref": plain_skill.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 1))
            assert no_runner.error_code == "skill_runner_unavailable"
            assert report.provider_ref is not None
            unexposed = await execute.invoke({"skill_ref": needy.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 2))
            assert unexposed.error_code == "skill_tool_not_exposed"
            runtime.arp.skill_use.script_runner = ScriptedRunner([receipt(output=b'{"n": 1}')])
            partial_result = await execute.invoke({"skill_ref": partial.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 3))
            assert partial_result.error_code == "skill_skill_manifest_conflict"
            assert store.read_skill_uses(runtime.uow.database.connection, _session(runtime, agent).session_id) == ()

    _run(case())
