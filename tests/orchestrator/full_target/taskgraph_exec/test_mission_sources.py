# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 §10: the orchestrator's Mission source reader for ARP MISSION pools.

Real Orchestrator, real strict-graph enable, real Worker Attempt through the original
dispatch path; only model replies are scripted.  While the Worker's model call is in
flight (the moment a MISSION Session freezes its request) the reader binds the exact
role-typed sources of the real dispatch intent and re-checks them; forged callers,
moved sources, a paused task and a stopped intent are refused by name.  Nothing is
replaced: the probes call the reader against the live store.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from production_fixture import enabled_world
from test_execution_view import _run_worker
from agent_orchestrator.orchestrator.state_machine import next_task
from agent_orchestrator.runtime.mission_sources import MissionSourceReader
from agent_orchestrator.runtime.native_plane import intent_caller
from agent_orchestrator.testing.fixtures import envelope_step
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import TrustedCaller
from simple_harness.agents.arp.strict import digest

OWNER = Pin("policy", "owner-mission-pool", 1, "0" * 64)


def _caller(intent):
    return intent_caller(intent, principal_id="host:user", owner_contract_ref=OWNER)


class _Discard(Exception):
    pass


def _code(call) -> str:
    with pytest.raises(ArpError) as refused:
        call()
    return refused.value.code


def test_worker_sources_are_exact_current_and_refused_when_they_move(tmp_path):
    seen: dict = {}

    def captured(probe):
        def run(request):
            try:
                return probe(request)
            except BaseException as error:  # surfaced below instead of a stalled Worker
                seen.setdefault("errors", []).append(repr(error) + repr(getattr(error, "detail", None)))
                raise
        return run

    def probe_first(request):
        store = seen["world"].loop.store
        [intent_id] = [r[0] for r in store.connection.execute("SELECT intent_id FROM dispatch_intents WHERE kind='attempt'")]
        intent = store.get_intent(intent_id)
        reader = seen["reader"]
        sources = reader.bind(caller=_caller(intent), role="root")
        reader.require_current(sources)  # the live Worker's sources are current
        seen.update(sources=sources, state=intent.state)

        # forged callers: another Mission, an unknown intent, a non-dispatch receipt
        seen["wrong_mission"] = _code(lambda: reader.bind(caller=_caller(replace(intent, mission_id="mission-other")), role="root"))
        seen["unknown_intent"] = _code(lambda: reader.bind(caller=_caller(replace(intent, intent_id="intent-missing")), role="root"))
        control = TrustedCaller(Pin("principal", "host:user", 0, digest("u")), OWNER,
                                Pin("receipt", "host:control:x", 0, digest("x")))
        seen["not_dispatch"] = _code(lambda: reader.bind(caller=control, role="root"))

        # moved sources: another occurrence, another manifest, an older generation
        other_occurrence = {**sources, "occurrence": {**sources["occurrence"], "occurrence_id": "occurrence-other"}}
        seen["occurrence"] = _code(lambda: reader.require_current(other_occurrence))
        manifest = sources["input_manifest"]
        stale_input = {**sources, "input_manifest": {**manifest, "manifest_hash": "f" * 64}}
        seen["input"] = _code(lambda: reader.require_current(stale_input))
        old_generation = {**sources, "input_manifest": {**manifest, "binding": {
            **manifest["binding"], "dispatch_generation": manifest["binding"]["dispatch_generation"] - 1}}}
        seen["generation"] = _code(lambda: reader.require_current(old_generation))

        # the task is paused (its execution authority withdrawn) — checked inside a write
        # transaction that is then rolled back, so the running Worker is not disturbed
        task_id = sources["attempt"]["task_id"]
        try:
            with store.transaction():
                task = store.get_task(task_id)
                store.update_task(next_task(task, paused=True, pause_reason="test"), expected_version=task.version)
                seen["paused"] = _code(lambda: reader.require_current(sources))
                raise _Discard
        except _Discard:
            pass
        reader.require_current(sources)
        return ("workspace_write_file", {"path": "facts.md", "content": "Repository facts."})

    def probe_second(request):
        store = seen["world"].loop.store
        sources = seen["sources"]
        seen["reader"].require_current(sources)  # still current after the tool call
        # an unrelated branch changes (the sibling occurrence's task is paused): this
        # Worker's sources stay current — rolled back afterwards
        sibling = store.connection.execute(
            "SELECT task_id FROM tasks WHERE mission_id=? AND task_id<>? AND status='READY' ORDER BY ordinal LIMIT 1",
            (sources["mission_id"], sources["attempt"]["task_id"])).fetchone()[0]
        try:
            with store.transaction():
                task = store.get_task(sibling)
                store.update_task(next_task(task, paused=True, pause_reason="test"), expected_version=task.version)
                seen["reader"].require_current(sources)
                seen["sibling_paused_ok"] = True
                raise _Discard
        except _Discard:
            pass
        step = envelope_step(summary="Recorded repository facts.", artifacts=["facts.md"], claims=[],
                             override=lambda value: {**value, "outputs": {"facts": "facts.md"}})
        return step(request) if callable(step) else step

    async def case():
        async with enabled_world(tmp_path, key="tg-mission-sources", worker_steps=(captured(probe_first), captured(probe_second))) as world:
            seen["world"] = world
            seen["reader"] = MissionSourceReader(lambda: world.loop)
            await world.commit_seed()
            try:
                await _run_worker(world)
            finally:
                assert not seen.get("errors"), seen.get("errors")
            sources = seen["sources"]
            assert seen["state"] in {"AGENT_CREATED", "SUBMITTED"}
            assert sources["source_kind"] == "worker" and sources["mission_id"] == world.mission.id
            assert sources["occurrence"]["occurrence_id"] and sources["occurrence"]["plan_revision"] == 1
            binding = sources["input_manifest"]["binding"]
            assert sources["input_manifest"]["manifest_hash"] == binding["manifest_hash"]
            assert binding["occurrence_id"] == sources["occurrence"]["occurrence_id"]
            assert binding["intent_id"] == sources["intent_id"] and isinstance(binding["dispatch_generation"], int)
            assert sources["frozen_config"]["task_version"] == sources["attempt"]["task_version"]
            assert (seen["wrong_mission"], seen["unknown_intent"], seen["not_dispatch"]) == (
                "REF_IDENTITY_MISMATCH", "SOURCE_UNAVAILABLE", "AUTHORITY_SOURCE_MISSING")
            assert seen["occurrence"] == seen["input"] == seen["generation"] == "REQUEST_SOURCE_STALE"
            assert seen["paused"] == "GENERATION_STALE"
            assert seen["sibling_paused_ok"] is True
            # the Attempt settled: its sources can no longer admit a request
            assert _code(lambda: seen["reader"].require_current(sources)) == "REQUEST_SOURCE_STALE"
            # the planner has no Worker Attempt and says so instead of borrowing one
            planner = world.loop.store.get_intent(world.intent.intent_id)
            planned = seen["reader"].bind(caller=_caller(planner), role="root")
            assert planned["source_kind"] == "planner"
            assert planned["input_manifest"] == {"not_applicable": "planner_has_no_worker_attempt"}
            assert MissionSourceReader(lambda: None) is not None
            assert _code(lambda: MissionSourceReader(lambda: None).require_current(sources)) == "SOURCE_UNAVAILABLE"

    asyncio.run(case())


def test_a_review_package_of_another_purpose_is_refused(monkeypatch):
    from test_htn_store import review_package
    from agent_orchestrator.contracts.resolution import ReviewPurpose
    from agent_orchestrator.storage.htn_store import HtnStore

    package = review_package(purpose=ReviewPurpose.MISSION_FINAL)
    monkeypatch.setattr(HtnStore, "get_review_package", lambda self, package_id: package)
    mission_id = package.binding.mission_id

    def intent(role):
        return SimpleNamespace(intent_id="intent-r", mission_id=mission_id, kind="plan", subject_id="s", creation_key="k",
                               input_id="i", input_hash="a" * 64, state="CLAIMED",
                               config={"role": role, "review_package_id": "package-1", "review_criteria": ["c-1"]})

    rows = {}
    store = SimpleNamespace(read_view=nullcontext, get_intent=lambda i: rows[i],
                            get_mission=lambda m: SimpleNamespace(id=m, tenant_id="t", status="ACTIVE", version=1))
    reader = MissionSourceReader(lambda: SimpleNamespace(store=store, commit=None))
    rows["intent-r"] = intent("root_reviewer")
    sources = reader.bind(caller=_caller(rows["intent-r"]), role="root")
    assert sources["source_kind"] == "reviewer"
    assert sources["review"]["purpose"] == "MISSION_FINAL" and sources["review"]["criteria"] == ["c-1"]
    assert sources["input_manifest"] == {"not_applicable": "reviewer_reads_its_review_package"}
    rows["intent-r"] = intent("operation_proposal_reviewer")  # a proposal reviewer handed a final package
    with pytest.raises(ArpError) as refused:
        reader.bind(caller=_caller(rows["intent-r"]), role="root")
    assert refused.value.code == "REF_IDENTITY_MISMATCH" and refused.value.detail["purpose"] == "MISSION_FINAL"


def test_the_final_judge_binds_its_judgment_view_not_an_attempt():
    """The Mission's final-judgment Critic names a judgment view as its ``attempt_id``; the
    reader binds that view through the orchestrator's own judge check (review finding:
    it used to be refused as a forged Attempt and stall the loop)."""
    mission_id = "mission-j"
    view_id = f"{mission_id}-judge-orchestrator-1"
    intent = SimpleNamespace(intent_id="intent-judge", mission_id=mission_id, kind="critic", subject_id=f"{mission_id}:judge:1",
                             creation_key="k", input_id="attempt-input", input_hash="a" * 64, state="CLAIMED",
                             config={"attempt_id": view_id, "role": None})
    workspace = {"kind": "judge", "mission_id": mission_id, "attempt_id": None, "state": "ACTIVE", "detail": {"artifacts": ["art-1"]}}

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    store = SimpleNamespace(
        read_view=nullcontext, get_intent=lambda i: intent,
        get_mission=lambda m: SimpleNamespace(id=m, tenant_id="t", status="ACTIVE", version=1),
        get_attempt=lambda a: None,
        get_workspace=lambda w: workspace if w == f"{view_id}-verify" else None,
        get_artifact=lambda a: SimpleNamespace(mission_id=mission_id, task_id="task-1"),
        connection=SimpleNamespace(execute=lambda sql, args: _Rows([(None, None)])),
    )
    reader = MissionSourceReader(lambda: SimpleNamespace(store=store, commit=None))
    sources = reader.bind(caller=_caller(intent), role="root")
    assert sources["source_kind"] == "critic" and sources["mission_judge"] == {
        "view_id": view_id, "task_ids": ["task-1"], "artifacts": ["art-1"]}
    workspace["kind"] = "work"  # not a registered judgment: refused by name, not a crash
    assert _code(lambda: reader.bind(caller=_caller(intent), role="root")) == "REF_IDENTITY_MISMATCH"


def test_a_refused_agent_creation_stops_that_intent_only_never_the_loop(tmp_path):
    """Review finding: a named native-plane refusal raised out of ``_dispatch`` and broke
    every cycle.  Now the intent is noted once and the loop keeps running."""
    async def case():
        async with enabled_world(tmp_path, key="tg-refused-create", worker_steps=()) as world:
            await world.commit_seed()
            loop = world.loop
            original = loop.bridge_for

            class _Refusing:
                def __init__(self, inner):
                    self.inner = inner

                async def create(self, **kwargs):
                    raise ArpError("REQUEST_SOURCE_STALE", "moved")

                def __getattr__(self, name):
                    return getattr(self.inner, name)

            loop.bridge_for = lambda intent: _Refusing(original(intent)) if intent.kind == "attempt" else original(intent)
            for _ in range(4):
                await loop._cycle()  # never raises
            row = loop.store.connection.execute(
                "SELECT state FROM dispatch_intents WHERE mission_id=? AND kind='attempt'", (world.mission.id,)).fetchone()
            assert row is not None and row[0] == "CLAIMED"
            assert len(loop._creation_refusals_noted) == 1 and world.provider.by_role.get("worker", 0) == 0

    asyncio.run(case())
