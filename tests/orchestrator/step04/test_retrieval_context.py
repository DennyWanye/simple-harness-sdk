# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · slice B (D4-9…D4-13, D4-19): deterministic retrieval, the eleven-item context
package under the visibility templates, deterministic summaries, the Mission boundary
(S4-06), explicit degrade / block on retrieval failure (S4-07) and the untrusted marking
of external content (S4-08); S4-02 end to end on fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fixtures_provider import graph_proposal_step
from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    passed_layers,
    submit,
    two_branch_service,
)

from agent_orchestrator.context.compression import GLOBAL_BRANCH, branch_of
from agent_orchestrator.context.context_builder import (
    CONTEXT_BUILDER_VERSION,
    assert_no_secrets,
    build_critic_package,
    build_worker_package,
)
from agent_orchestrator.context.retrieval import (
    RETRIEVAL_VERSION,
    KnowledgeContext,
    RetrievalResult,
    rank_knowledge,
)
from agent_orchestrator.contracts import (
    AttemptStatus,
    Budget,
    ClaimStatus,
    MissionStatus,
    Task,
    TaskStatus,
)
from agent_orchestrator.memory.summaries import build_summaries
from agent_orchestrator.memory.verified_knowledge import KnowledgeRecord
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.tool_gateway import UNTRUSTED_NOTICE
from agent_orchestrator.testing.fixtures import (
    COMPARE_SEED,
    COMPARE_SPEC,
    COMPARE_TASKS,
    compare_script_a,
    compare_script_b,
    compare_script_c,
    demo_knowledge_sharing_provider,
    knowledge_envelope_step,
    typed_claim,
)


def _task(task_id, deps=(), goal="检查 impl_a 的空输入", mission="m", kind="work"):
    return Task(
        id=task_id,
        mission_id=mission,
        parent_task_ids=(),
        dependency_ids=tuple(deps),
        goal=goal,
        rationale="r",
        success_criteria=("pytest:tests/x.py",),
        verification_policy=("format_check",),
        allowed_tools=(),
        budget=Budget(max_tokens=10),
        priority=1.0,
        status=TaskStatus.READY,
        version=1,
        kind=kind,
    )


def _record(
    kid,
    *,
    source,
    content,
    key=None,
    stance="affirms",
    status="VERIFIED",
    created=1.0,
    used_by=(),
    mission="m",
    superseded_by=None,
):
    return KnowledgeRecord(
        id=kid,
        mission_id=mission,
        claim_id=kid,
        content=content,
        type="statement",
        status=status,
        version=1,
        key=key,
        stance=stance,
        proposed_by="agent",
        source_task=source,
        source_attempt=f"{source}:attempt-1",
        source_result="r",
        evidence=("pytest:tests/x.py",),
        verifier={"layer": "code_test"},
        dependencies=(),
        created_at=created,
        used_by=tuple(used_by),
        superseded_by=superseded_by,
    )


# ------------------------------------------------------------------ D4-9
def test_ranking_combines_relevance_trust_distance_recency_reuse_and_drops_duplicates_and_superseded():
    tasks = {
        "m:task-1": _task("m:task-1"),
        "m:task-2": _task("m:task-2"),
        "m:task-3": _task(
            "m:task-3", deps=("m:task-1",), goal="综合 impl_a 空输入 与 impl_b 的比较"
        ),
        "m:task-4": _task("m:task-4", deps=("m:task-2",), goal="别的"),
    }
    records = [
        _record(
            "K-anc",
            source="m:task-1",
            content="impl_a 对空输入抛错",
            key="impl_a.empty_input",
            stance="refutes",
            created=1.0,
        ),
        _record(
            "K-far",
            source="m:task-4",
            content="impl_a 对空输入抛错（复制）",
            key="impl_a.empty_input",
            stance="refutes",
            created=2.0,
        ),
        _record(
            "K-other",
            source="m:task-2",
            content="impl_b 尾分隔符抛错",
            key="impl_b.trailing",
            stance="refutes",
            created=3.0,
            used_by=("m:task-9", "m:task-8"),
        ),
        _record(
            "K-old",
            source="m:task-1",
            content="旧结论",
            key="impl_a.trailing",
            status="SUPERSEDED",
            superseded_by="K-new",
        ),
        _record("K-new", source="m:task-1", content="新结论", key="impl_a.trailing", created=4.0),
        _record("K-foreign", source="x:task-1", content="impl_a 对空输入抛错", mission="x"),
    ]
    result = rank_knowledge(tasks["m:task-3"], records, tasks_by_id=tasks, limit=10)
    assert result.version == RETRIEVAL_VERSION and result.status == "ok"
    ids = [item.id for item in result.items]
    assert (
        "K-foreign" not in ids and result.considered == 5
    )  # permission pre-filter: same Mission only
    assert "K-old" not in ids and result.dropped["superseded"] == ["K-old"]
    assert result.superseded[0]["superseded_by"] == "K-new"
    assert (
        "K-far" in result.dropped["duplicate"] and "K-anc" in ids
    )  # same key+stance: the ancestor wins
    assert ids[0] == "K-anc"  # most relevant + ancestor
    parts = {item.id: item.parts for item in result.items}
    assert parts["K-anc"]["proximity"] == 1.0 and parts["K-other"]["proximity"] < 1.0
    assert parts["K-other"]["reuse"] == pytest.approx(2 / 3)
    assert rank_knowledge(tasks["m:task-3"], records, tasks_by_id=tasks, limit=1).dropped[
        "over_limit"
    ]
    assert rank_knowledge(tasks["m:task-3"], [], tasks_by_id=tasks).items == ()


# ------------------------------------------------------------------ D4-10'
def _mission():
    from agent_orchestrator.contracts import Mission

    return Mission(
        id="m",
        goal="比较两个实现",
        success_criteria=("pytest:tests/test_comparison.py",),
        stop_conditions=(),
        allowed_tools=("run_tests",),
        risk_level="sandbox",
        budget=Budget(max_tokens=100),
        tenant_id="t",
        status=MissionStatus.ACTIVE,
        created_at=1.0,
        version=1,
        idempotency_key="k",
    )


def _attempt(task):
    from agent_orchestrator.contracts import Attempt

    return Attempt(
        id=f"{task.id}:attempt-1",
        task_id=task.id,
        mission_id="m",
        role="worker",
        model="x",
        prompt_version="v",
        context_version="pending",
        budget_reserved=Budget(max_tokens=5),
        lease_owner=None,
        lease_expires_at=None,
        status=AttemptStatus.PENDING,
        retry_of=None,
        created_at=1.0,
        version=1,
        ordinal=1,
        creation_key="c",
        input_id="i",
    )


def _knowledge_context():
    verified = (
        {
            "id": "K-1",
            "version": 1,
            "status": "VERIFIED",
            "key": "impl_a.empty_input",
            "stance": "refutes",
            "content": "impl_a 抛错",
            "verifier": {"layer": "code_test"},
        },
    )
    disputed = (
        {
            "claim_id": "C-9",
            "status": "DISPUTED",
            "key": "impl_a.empty_input",
            "stance": "affirms",
            "content": "impl_a 返回 {}",
            "marker": "DISPUTED — 争议中，不是事实",
        },
    )
    candidates = (
        {
            "claim_id": "C-2",
            "status": "SUPPORTED",
            "content": "impl_a 风格好",
            "marker": "UNVERIFIED — 候选结论，不能当作事实",
        },
    )
    rejected = ({"claim_id": "C-3", "status": "REJECTED", "content": "错的"},)
    return KnowledgeContext(
        retrieval=RetrievalResult(RETRIEVAL_VERSION, "ok", (), (), {}, 3),
        verified=verified,
        disputed=disputed,
        candidates=candidates,
        rejected=rejected,
        branch_summary={"scope": "branch", "version": "sum-1"},
        global_summary={"scope": "mission", "version": "sum-2"},
    )


def test_worker_package_has_all_eleven_items_and_shows_only_verified_as_fact():
    task = _task("m:task-2", deps=("m:task-1",))
    package = build_worker_package(
        _mission(),
        task,
        _attempt(task),
        previous_attempts=(),
        verifier_feedback=({"layer": "code_test", "summary": "1 failed"},),
        workspace_files=("a.py",),
        dependencies=(
            {"task_id": "m:task-1", "goal": "g", "status": "COMPLETED", "accepted_artifacts": []},
        ),
        knowledge=_knowledge_context(),
        untrusted_sources=("docs/",),
    )
    body = package.package
    assert body["context_builder_version"] == CONTEXT_BUILDER_VERSION
    assert body["mission_root_goal"] and body["task_contract"]["task_id"] == task.id  # 1, 2
    assert body["dependencies"][0]["task_id"] == "m:task-1"  # 3
    assert body["branch_summary"]["version"] == "sum-1"  # 4
    assert (
        body["verified_knowledge"][0]["id"] == "K-1"
        and body["verified_knowledge"][0]["version"] == 1
    )  # 5
    assert (
        body["failure_history"] == [] and body["verifier_feedback"][0]["layer"] == "code_test"
    )  # 6, 8
    assert body["disputed_claims"][0]["marker"].startswith("DISPUTED")  # 7, marked
    assert body["tools_and_permissions"]["untrusted_sources"] == ["docs/"]  # 9
    assert body["budget"]["reserved"]["max_tokens"] == 5 and body["output_contract"].startswith(
        "<result_envelope>"
    )  # 10, 11
    assert (
        "candidate_claims" not in body and "rejected_claims" not in body
    )  # worker never sees candidates
    assert "impl_a 风格好" not in package.text
    assert body["knowledge_retrieval"]["retrieval_version"] == RETRIEVAL_VERSION
    assert package.context_version.startswith("ctx-")


def test_verifier_and_critic_templates_withhold_the_submitter_and_arbiter_sees_the_dispute():
    task = _task("m:task-2")
    verifier = build_critic_package(
        _mission(),
        task,
        attempt_id="a",
        artifacts=({"path": "x.py", "content_hash": "0" * 64, "size_bytes": 1},),
        test_output="1 passed",
        workspace_files=("x.py",),
        knowledge=_knowledge_context(),
    ).package

    def keys_of(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys_of(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys_of(item)

    assert not {"summary", "confidence", "self_reported_confidence"} & set(keys_of(verifier))
    assert verifier["visibility"].startswith("verifier") and "candidate_claims" not in verifier
    assert verifier["disputed_claims"][0]["status"] == "DISPUTED"
    critic = build_critic_package(
        _mission(),
        _task("m:task-5", kind="conflict"),
        attempt_id="a",
        artifacts=(),
        test_output=None,
        workspace_files=(),
        knowledge=_knowledge_context(),
        visibility="critic",
    ).package
    assert (
        critic["candidate_claims"][0]["marker"].startswith("UNVERIFIED")
        and critic["rejected_claims"]
    )
    assert "dispute" in critic
    conflict = Task.from_json(
        {
            **_task("m:task-5", kind="conflict").to_json(),
            "context": {"key": "impl_a.empty_input", "claim_ids": ["K-1", "C-9"]},
        }
    )
    arbiter = build_worker_package(
        _mission(),
        conflict,
        _attempt(conflict),
        previous_attempts=(),
        verifier_feedback=(),
        workspace_files=(),
        knowledge=_knowledge_context(),
        role="arbiter",
    ).package
    assert arbiter["dispute"]["key"] == "impl_a.empty_input" and arbiter["visibility"].startswith(
        "arbiter"
    )
    synth = build_worker_package(
        _mission(),
        _task("m:task-6", kind="synthesis"),
        _attempt(_task("m:task-6")),
        previous_attempts=(),
        verifier_feedback=(),
        workspace_files=(),
        knowledge=_knowledge_context(),
        role="synthesizer",
    ).package
    assert synth["global_summary"]["version"] == "sum-2" and synth["visibility"].startswith(
        "synthesizer"
    )


def test_retrieval_unavailable_is_rendered_explicitly_and_secrets_never_enter_a_package():
    task = _task("m:task-1")
    package = build_worker_package(
        _mission(),
        task,
        _attempt(task),
        previous_attempts=(),
        verifier_feedback=(),
        workspace_files=(),
        knowledge=KnowledgeContext.unavailable("index not ready"),
    )
    body = package.package
    assert (
        body["knowledge_retrieval"]["status"] == "unavailable"
        and body["knowledge_retrieval"]["reason"] == "index not ready"
    )
    assert "不代表没有相关知识" in package.text and "没有证据" not in body["knowledge_retrieval"][
        "note"
    ].replace("不要把'未检索到'当成'没有证据'", "")
    assert body["verified_knowledge"] == [] and body["branch_summary"]["status"] == "unavailable"
    disabled = build_worker_package(
        _mission(),
        task,
        _attempt(task),
        previous_attempts=(),
        verifier_feedback=(),
        workspace_files=(),
        knowledge=KnowledgeContext.unavailable("off", status="disabled"),
    ).package
    assert disabled["knowledge_retrieval"]["status"] == "disabled"
    with pytest.raises(ValueError):
        assert_no_secrets({"tools": {"api_key": "x"}})
    assert_no_secrets({"budget": {"max_tokens": 5}})  # not a credential


# ------------------------------------------------------------------ D4-13'
def test_summaries_are_deterministic_scoped_by_branch_and_never_change_claim_status(tmp_path):
    service, mission, (task_a, task_b) = two_branch_service(tmp_path)
    tasks_by_id = {t.id: t for t in service.store.list_tasks(mission.id)}
    assert branch_of(task_a, tasks_by_id) == task_a.id
    join = Task.from_json(
        {**task_a.to_json(), "id": "m:task-9", "dependency_ids": [task_a.id, task_b.id]}
    )
    assert branch_of(join, {**tasks_by_id, join.id: join}) == GLOBAL_BRANCH
    attempt = drive_to_running(service, task_a)
    stored = submit(
        service,
        attempt,
        envelope(
            attempt,
            claims=[
                claim("v", key="k", evidence=["pytest:tests/probe/test_impl_a.py"]),
                claim("只有证据"),
            ],
        ),
    )
    service.accept_result(
        stored.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    persisted = {s["subject_id"]: s for s in service.store.list_summaries(mission.id)}
    assert set(persisted) == {task_a.id, task_b.id, mission.id}
    branch = persisted[task_a.id]
    assert branch["knowledge"] and branch["uncertainty"]["unverified_claims"] == 1
    assert branch["sources"]["results"] == [stored.envelope.id]
    assert "不是验证" in branch["uncertainty"]["note"]
    assert persisted[task_b.id]["knowledge"] == []  # B's branch does not carry A's knowledge
    rebuilt = build_summaries(service.store, mission.id)
    assert rebuilt[task_a.id]["version"] == branch["version"]  # deterministic
    statuses_before = {c.id: c.status for c in service.store.list_mission_claims(mission.id)}
    build_summaries(service.store, mission.id)
    assert {
        c.id: c.status for c in service.store.list_mission_claims(mission.id)
    } == statuses_before


# ------------------------------------------------------------------ closure helpers
def spec(key, **overrides):
    # slice-B closures run without the synthesis Task: the Mission criterion is a seed file
    base = dict(
        goal=COMPARE_SPEC["goal"],
        success_criteria=("file:contract/CONTRACT.md",),
        tenant_id="tenant-4",
        idempotency_key=key,
        allowed_tools=tuple(COMPARE_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=200_000, max_attempts=12),
        workspace_seed=COMPARE_SEED,
        untrusted_sources=("docs/",),
    )
    base.update(overrides)
    return MissionSpec(**base)


def config(tmp_path, **overrides):
    base = dict(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def by_goal(store, mission_id):
    goals = {t["goal"]: t["key"] for t in COMPARE_TASKS}
    return {goals[t.goal]: t for t in store.list_tasks(mission_id) if t.goal in goals}


def only(keys):
    return [t for t in COMPARE_TASKS if t["key"] in keys]


# ------------------------------------------------------------------ S4-02
def test_s4_02_unverified_claims_never_reach_a_worker_as_fact_and_citing_one_fails(tmp_path):
    store_ref = {}

    def cite_supported(package):  # the Worker somehow learned A's claim id and cites it
        store = store_ref["store"]
        claims = [
            c
            for c in store.list_mission_claims(store_ref["mission"])
            if c.status is ClaimStatus.SUPPORTED
        ]
        return [claims[0].id]

    provider = demo_knowledge_sharing_provider(
        tasks=only("AB"),
        scripts={
            "A": compare_script_a(verified=False),
            "B": compare_script_b(cite=cite_supported) + compare_script_b(cite=True),
        },
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-02"))
            store_ref.update(store=orchestrator.store, mission=mission.id)
            await orchestrator.run()
            store = orchestrator.store
            tasks = by_goal(store, mission.id)
            a_result = store.get_result(tasks["A"].accepted_result_id)
            a_claims = store.list_claims(a_result.envelope.id)
            assert {c.status for c in a_claims} == {
                ClaimStatus.SUPPORTED
            }  # confident, no test coverage
            assert all(k.source_task != tasks["A"].id for k in store.list_knowledge(mission.id))
            b_attempts = store.list_attempts(tasks["B"].id)
            assert [a.status for a in b_attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            first_intent = store.get_intent_for_subject(b_attempts[0].id)
            assert first_intent.config["knowledge"] == []  # nothing VERIFIED was offered
            assert first_intent.config["context_builder_version"] == CONTEXT_BUILDER_VERSION
            message = str(first_intent.config["message"])
            assert (
                "impl_a 对空输入抛 ValueError" not in message
            )  # the SUPPORTED statement is not in B's context
            failure = b_attempts[0].failure["failures"][0]
            assert failure["layer"] == "rule_check" and "not VERIFIED" in failure["summary"]
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )

    asyncio.run(case())


# ------------------------------------------------------------------ S4-06
def test_s4_06_knowledge_and_agents_never_cross_the_mission_boundary(tmp_path):
    provider = demo_knowledge_sharing_provider(
        tasks=only("AB"),
        planner_steps=[graph_proposal_step(only("AB")), graph_proposal_step(only("AB"))],
        per_attempt={
            "A": [compare_script_a(), compare_script_a()],
            "B": [compare_script_b(), compare_script_b()],
        },
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            first = await orchestrator.submit_mission(spec("s4-06-a"))
            second = await orchestrator.submit_mission(spec("s4-06-b"))
            await orchestrator.run()
            store = orchestrator.store
            agent_ids = []
            knowledge_of = {
                m.id: {k.id for k in store.list_knowledge(m.id)} for m in (first, second)
            }
            assert knowledge_of[first.id] and knowledge_of[second.id]
            assert knowledge_of[first.id].isdisjoint(knowledge_of[second.id])
            for mission in (first, second):
                assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                    orchestrator.progress_log
                )
                own = knowledge_of[mission.id]
                assert all(k.mission_id == mission.id for k in store.list_knowledge(mission.id))
                for task in store.list_tasks(mission.id):
                    for attempt in store.list_attempts(task.id):
                        agent_ids.append(attempt.agent_id)
                        intent = store.get_intent_for_subject(attempt.id)
                        offered = {item["id"] for item in intent.config["knowledge"]}
                        assert offered <= own
                        other = first.id if mission is second else second.id
                        assert other not in str(intent.config["message"]["content"])
                b_result = store.get_result(by_goal(store, mission.id)["B"].accepted_result_id)
                assert (
                    set(b_result.envelope.used_knowledge) <= own
                    and b_result.envelope.used_knowledge
                )
            assert (
                len(agent_ids) == len(set(agent_ids)) == 4
            )  # a fresh BaseAgent per Attempt: no shared history

    asyncio.run(case())


# ------------------------------------------------------------------ S4-07
def test_s4_07_block_policy_is_visible_persistent_and_bounded(tmp_path):
    provider = demo_knowledge_sharing_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(
            config(tmp_path, max_retrieval_failures=3), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-07-block"))
            orchestrator.arm_fault("retrieval_unavailable", kind="attempt", times=3)
            await orchestrator.run()
            store = orchestrator.store
            task = store.list_tasks(mission.id)[0]
            events = [e for e in store.list_events(mission.id) if e.type == "RetrievalUnavailable"]
            assert [e.payload["count"] for e in events] == [1, 2, 3] and all(
                e.payload["policy"] == "block" for e in events
            )
            assert store.list_attempts(task.id) == []  # blocked: no Attempt was created
            assert (
                task.status is TaskStatus.FAILED and task.failure_reason == "retrieval_unavailable"
            )
            final = store.get_mission(mission.id)
            assert (
                final.status is MissionStatus.FAILED
                and final.stop_reason == "retrieval_unavailable"
            )
            assert provider.by_role.get("worker", 0) == 0

    asyncio.run(case())


def test_s4_07_block_once_then_recover_keeps_the_task_ready_and_counts_across_restarts(tmp_path):
    provider = demo_knowledge_sharing_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(
            config(tmp_path, max_retrieval_failures=3), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-07-once"))
            orchestrator.arm_fault("retrieval_unavailable", kind="attempt", times=1)
            await orchestrator.run()
            store = orchestrator.store
            assert store.count_events(mission.id, "RetrievalUnavailable") == 1
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
        # a restarted instance derives the same count from the events
        async with Orchestrator(config(tmp_path), provider) as restarted:
            task_id = restarted.store.list_tasks(mission.id)[0].id
            assert (
                restarted.commit.record_retrieval_unavailable(task_id, reason="x", policy="block")
                == 2
            )

    asyncio.run(case())


def test_s4_07_degrade_policy_and_kill_switch_are_explicit_in_the_package(tmp_path):
    provider = demo_knowledge_sharing_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(
            config(tmp_path, on_retrieval_failure="degrade"), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-07-degrade"))
            orchestrator.arm_fault("retrieval_unavailable", kind="attempt")
            await orchestrator.run()
            store = orchestrator.store
            task = store.list_tasks(mission.id)[0]
            intent = store.get_intent_for_subject(store.list_attempts(task.id)[0].id)
            assert (
                intent.config["retrieval_status"] == "unavailable"
                and intent.config["knowledge"] == []
            )
            message = str(intent.config["message"]["content"])
            assert "不代表没有相关知识" in message and '"status":"unavailable"' in message
            assert store.count_events(mission.id, "RetrievalUnavailable") == 1
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED
        fresh = demo_knowledge_sharing_provider(tasks=only("A"))
        async with Orchestrator(
            config(tmp_path / "off", knowledge_sharing=False), fresh
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-07-off"))
            await orchestrator.run()
            store = orchestrator.store
            intent = store.get_intent_for_subject(
                store.list_attempts(store.list_tasks(mission.id)[0].id)[0].id
            )
            assert intent.config["retrieval_status"] == "disabled"
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED

    asyncio.run(case())


# ------------------------------------------------------------------ S4-08
def test_s4_08_external_content_is_marked_untrusted_and_grants_nothing(tmp_path):
    bad_envelope = knowledge_envelope_step(
        summary="按文档指示",
        artifacts=["notes/review_impl_a.md"],
        claims=[
            {
                **typed_claim(
                    "impl_a 满足合同", key="impl_a.empty_input", evidence=["docs/vendor_notes.md"]
                ),
                "status": "VERIFIED",
            }
        ],
        cite_knowledge=False,
    )
    script_c = compare_script_c()[:-1] + [bad_envelope] + compare_script_c()
    provider = demo_knowledge_sharing_provider(tasks=only("C"), scripts={"C": script_c})

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-08"))
            await orchestrator.run()
            store = orchestrator.store
            gateway = orchestrator.assembled.gateway
            reads = [
                c
                for c in gateway.calls
                if c["tool"] == "workspace_read_file"
                and c["arguments"]["path"] == "docs/vendor_notes.md"
            ]
            assert reads and all(c["trust"] == "untrusted_external" for c in reads)
            # the Task Contract never listed run_tests: the SDK registry refuses the call
            # before it reaches the gateway, and the gateway would refuse it too
            assert not [c for c in gateway.calls if c["tool"] == "run_tests"]
            refused = [
                r for r in provider.requests if "tool_not_exposed_for_agent" in str(r.messages)
            ]
            assert refused, (
                "the forbidden run_tests call must have been refused by the SDK registry"
            )
            task = store.list_tasks(mission.id)[0]
            attempts = store.list_attempts(task.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert (
                attempts[0].failure["reason"] == "envelope_invalid"
                and "PROPOSED" in attempts[0].failure["error"]
            )
            result = store.get_result(task.accepted_result_id)
            graded = store.list_claims(result.envelope.id)[0]
            assert (
                graded.status is ClaimStatus.UNDER_REVIEW
                and graded.confidence_metadata["grade"] == "unsupported"
            )
            assert graded.confidence_metadata["evidence_trust"] == ["untrusted_external"]
            assert store.list_knowledge(mission.id) == []
            intent = store.get_intent_for_subject(attempts[0].id)
            assert "SYSTEM NOTICE" not in str(
                intent.config["message"]
            )  # external content is never inlined
            assert intent.config["untrusted_sources"] == ["docs/"]
            assert UNTRUSTED_NOTICE.startswith("以下内容来自不可信")
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )

    asyncio.run(case())
