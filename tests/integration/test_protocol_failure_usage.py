# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real HTTP adapter -> BaseAgent -> SQLite failure/usage, with no model/network.

The transport returns malformed tool arguments. Parsing must still fail and no
tool may run; accounting is independent of that execution failure. Commit-created
subjects bind the real Provider admission guard. This is not a Host adapter test
or a substitute for Orchestrator automatic late-accounting recovery.
"""

import asyncio
import json

import httpx
import pytest

from agent_orchestrator.contracts import Budget, sha256_hex
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec, Reservation
from agent_orchestrator.runtime.agent_worker import AgentBridge, user_message_json
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.storage.store import Store
from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.providers import OpenAICompatibleProvider, ProviderProtocolError, Secret
from simple_harness.runtime.consumer_adapter import ConsumerRuntimePolicies
from simple_harness.tools import ToolResult

PROFILE = "default"


class InputBound:
    fingerprint = "protocol-usage-oracle-bound-v1"
    bound_protocol = "fixture-text-only-v1"
    requires_prior_output_reserve = True

    def estimate_input_tokens(self, request):
        return 100


class RecordingTool:
    def __init__(self):
        self.calls = []

    async def execute(self, call, context):
        self.calls.append(call)
        return ToolResult.succeeded(call.call_id, {"observed": True})


VALID_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "total_tokens": 150,
    "prompt_tokens_details": {"cached_tokens": 20},
    "completion_tokens_details": {"reasoning_tokens": 30},
}


@pytest.mark.parametrize(
    "usage,known",
    [
        pytest.param(VALID_USAGE, True, id="valid-usage-bad-tool-json"),
        pytest.param(None, False, id="missing-usage"),
        pytest.param({**VALID_USAGE, "prompt_tokens": True}, False, id="bool-token-count"),
        pytest.param({**VALID_USAGE, "total_tokens": 149}, False, id="inconsistent-total"),
        pytest.param(
            {**VALID_USAGE, "completion_tokens_details": {"reasoning_tokens": -1}},
            False,
            id="invalid-reasoning-count",
        ),
    ],
)
def test_protocol_failure_keeps_only_valid_usage_without_resampling_or_tools(
    tmp_path, usage, known
):
    async def exercise():
        calls = []

        def transport(request):
            # Do not record headers/credentials or make any real HTTP connection.
            calls.append(request.url.path)
            payload = {
                "id": "original-bad-tool-response",
                "model": "deepseek-flash",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "unparseable-call",
                                    "type": "function",
                                    "function": {"name": "probe", "arguments": '{"path": "never'},
                                }
                            ],
                        },
                    }
                ],
            }
            if usage is not None:
                payload["usage"] = usage
            return httpx.Response(200, json=payload)

        store = Store.open(tmp_path / "orchestrator.db")
        try:
            commit = CommitService(
                store,
                global_budget=Budget(
                    max_tokens=400_000,
                    max_cost_micros=40_000,
                    max_attempts=12,
                ),
            )
            mission, _ = commit.create_mission(
                MissionSpec(
                    goal="Read a tool response without concealing protocol errors",
                    success_criteria=("file:report.md",),
                    tenant_id="protocol-test",
                    idempotency_key="protocol-test",
                    allowed_tools=("probe",),
                    budget=Budget(max_tokens=200_000, max_cost_micros=20_000, max_attempts=6),
                )
            )
            planning = commit.begin_planning(mission.id)
            [task], _ = commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            {
                                "key": "A",
                                "goal": mission.goal,
                                "rationale": "Run the real SDK protocol and accounting path",
                                "dependencies": [],
                                "success_criteria": ["file:report.md"],
                                "verification_policy": ["format_check", "rule_check"],
                                "allowed_tools": ["probe"],
                                "outputs": ["report.md"],
                                "budget": {
                                    "max_tokens": 20_000,
                                    "max_cost_micros": 6000,
                                    "max_attempts": 1,
                                },
                            }
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "protocol integration fixture"},
            )
            price = FrozenPriceEstimator(
                "original-protocol-price", "consumer", 1_000_000, 2_000_000
            )
            guard = ProviderBudgetGuard(
                commit,
                owner="protocol-owner",
                estimator=InputBound(),
                max_slots=1,
                priced=True,
                price_tables={PROFILE: price},
            )
            tools = RecordingTool()
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                provider = OpenAICompatibleProvider(
                    client,
                    "https://protocol.invalid/v1",
                    "deepseek-flash",
                    Secret("fixture-only"),
                )
                ports = AgentRuntimePorts(
                    provider=provider,
                    authorization=AllowAllAuthorization(),
                    database_path=str(tmp_path / "execution.db"),
                    model="deepseek-flash",
                    provider_admission=guard,
                    tool_executor=tools,
                    tool_names=("probe",),
                    tool_schemas={
                        "probe": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                            "additionalProperties": False,
                        }
                    },
                    policies=ConsumerRuntimePolicies(
                        "consumer_supplied", False, "fail_closed", estimator=price
                    ),
                    default_max_output_tokens=1000,
                    # Keep normal empty-response retry capability enabled. A
                    # protocol error must not be silently reclassified as that case.
                    empty_response_retries=2,
                )
                async with build_agent_runtime(ports) as runtime:
                    agent = await runtime.create(
                        AgentConfig(
                            name="protocol-test",
                            instructions="Call probe once.",
                            model_profile_ref=PROFILE,
                        ),
                        creation_key="protocol-agent",
                    )
                    message = user_message_json("Read the requested file.")
                    attempt, intent = commit.create_attempt(
                        task.id,
                        role="worker",
                        model="deepseek-flash",
                        prompt_version="worker-v2",
                        context_version="protocol-fixture-v1",
                        reservation=Reservation(tokens=4000, cost_micros=0),
                        intent_config={
                            "runtime_profile_id": PROFILE,
                            "model": "deepseek-flash",
                            "agent_config": agent.config.to_json(),
                            "message": message,
                            "provider_admission_fingerprint": guard.fingerprint,
                        },
                        input_hash=sha256_hex(message),
                    )
                    commit.claim_intent(intent.intent_id, owner="protocol-owner", lease_seconds=60)
                    commit.record_agent_created(
                        intent.intent_id,
                        agent_id=agent.agent_id,
                        expected_turn_id=agent.turn_id_for(intent.input_id),
                    )
                    receipt = await agent.submit(message["content"], input_id=intent.input_id)
                    commit.record_submitted(
                        intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
                    )
                    result = await agent.wait_turn(receipt.turn_id, timeout=5)
                    # Distinguish a broken admission fixture from the intended
                    # protocol red: the actual HTTP adapter must have been reached.
                    assert calls == ["/v1/chat/completions"], result.error
                    assert str(result.state) == "failed"
                    assert result.error["error_code"] == ProviderProtocolError.error_code
                    [original] = runtime.uow.list_provider_invocations(RunId(agent.run_id))
                    assert str(original.state) == "failed" and original.handoff_attempt == 1
                    assert original.error_code == "provider_protocol_error"
                    assert original.response_json is None
                    assert original.estimator_digest == price.snapshot_digest
                    assert tools.calls == [] and calls == ["/v1/chat/completions"]
                    [grant] = list(store.connection.execute("SELECT * FROM provider_token_grants"))
                    facts = AgentBridge(runtime, unpriced=False).usage_facts(
                        agent_id=agent.agent_id
                    )
                    if known:
                        assert dict(original.usage_json["usage"]) == {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "total_tokens": 150,
                            "cache_tokens": 20,
                            "reasoning_tokens": 30,
                        }
                        assert str(original.budget_charge.kind) == "trusted_usage"
                        assert original.budget_charge.amount_micros == 200
                        assert (
                            runtime.uow.read_provider_budget(original.run_id).committed_micros
                            == 200
                        )
                        assert grant["state"] == "SETTLED"
                        assert grant["actual_tokens"] == 150 and grant["actual_cost_micros"] == 200
                        assert (
                            len(facts) == 1
                            and facts[0].tokens == 150
                            and facts[0].cost_micros == 200
                        )
                    else:
                        assert original.usage_json.get("usage") is None
                        assert str(original.budget_charge.kind) != "trusted_usage"
                        assert grant["state"] == "UNKNOWN"
                        assert (
                            grant["actual_tokens"] is None and grant["actual_cost_micros"] is None
                        )
                        assert facts == []
                        assert commit.ledger.has_unknown_usage(attempt.id)
                        assert commit.ledger.reservation(attempt.id)["state"] == "RESERVED"
                    # Public same-input replay and reconcile retain the original
                    # failed result and never cause an invisible second HTTP call.
                    repeated = await agent.submit(message["content"], input_id=intent.input_id)
                    assert repeated.turn_id == receipt.turn_id
                    assert await agent.wait_turn(repeated.turn_id, timeout=5) == result
                    await runtime.kernel.reconcile()
                    assert runtime.uow.read_provider_invocation(original.invocation_id) == original
                    assert len(runtime.uow.list_provider_invocations(original.run_id)) == 1
                    assert tools.calls == [] and calls == ["/v1/chat/completions"]
        finally:
            store.close()

    asyncio.run(exercise())


async def _exercise_priced_length_recovery(tmp_path, *, task_cost_micros: int) -> None:
    """Run a length retry through the real HTTP adapter and both SDK/Orch ledgers."""
    caps = []

    def transport(request):
        assert request.url.path == "/v1/chat/completions"
        cap = json.loads(request.content)["max_tokens"]
        caps.append(cap)
        if len(caps) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "truncated-tool",
                        "type": "function",
                        "function": {"name": "probe", "arguments": '{"path":"unfinished'},
                    }
                ],
            }
            output = cap
            finish_reason = "length"
        else:
            message = {"role": "assistant", "content": "complete"}
            output = 40
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"priced-response-{len(caps)}",
                "model": "deepseek-flash",
                "choices": [{"message": message, "finish_reason": finish_reason}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": output,
                    "total_tokens": 100 + output,
                },
            },
        )

    store = Store.open(tmp_path / "orchestrator.db")
    try:
        commit = CommitService(
            store,
            global_budget=Budget(max_tokens=400_000, max_cost_micros=40_000, max_attempts=12),
        )
        mission, _ = commit.create_mission(
            MissionSpec(
                goal="Recover a truncated tool response within the original task budget",
                success_criteria=("file:report.md",),
                tenant_id="length-protocol-test",
                idempotency_key="length-protocol-test",
                allowed_tools=("probe",),
                budget=Budget(max_tokens=200_000, max_cost_micros=20_000, max_attempts=6),
            )
        )
        planning = commit.begin_planning(mission.id)
        [task], _ = commit.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json(
                {
                    "tasks": [
                        {
                            "key": "A",
                            "goal": mission.goal,
                            "rationale": "Priced admission after truncated tool JSON",
                            "dependencies": [],
                            "success_criteria": ["file:report.md"],
                            "verification_policy": ["format_check", "rule_check"],
                            "allowed_tools": ["probe"],
                            "outputs": ["report.md"],
                            "budget": {
                                "max_tokens": 20_000,
                                "max_cost_micros": task_cost_micros,
                                "max_attempts": 1,
                            },
                        }
                    ]
                }
            ),
            base_version=planning.version,
            source={"planner": "priced length integration fixture"},
        )
        price = FrozenPriceEstimator("original-protocol-price", "consumer", 1_000_000, 2_000_000)
        guard = ProviderBudgetGuard(
            commit,
            owner="protocol-owner",
            estimator=InputBound(),
            max_slots=1,
            priced=True,
            price_tables={PROFILE: price},
        )
        tools = RecordingTool()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://protocol.invalid/v1", "deepseek-flash", Secret("fixture-only")
            )
            ports = AgentRuntimePorts(
                provider=provider,
                authorization=AllowAllAuthorization(),
                database_path=str(tmp_path / "execution.db"),
                model="deepseek-flash",
                provider_admission=guard,
                tool_executor=tools,
                tool_names=("probe",),
                tool_schemas={
                    "probe": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    }
                },
                policies=ConsumerRuntimePolicies(
                    "consumer_supplied", False, "fail_closed", estimator=price
                ),
                default_max_output_tokens=1000,
                max_output_tokens_ceiling=2000,
                empty_response_retries=2,
            )
            async with build_agent_runtime(ports) as runtime:
                agent = await runtime.create(
                    AgentConfig(
                        name="protocol-length-test",
                        instructions="Call probe once.",
                        model_profile_ref=PROFILE,
                    ),
                    creation_key="protocol-length-agent",
                )
                message = user_message_json("Read the requested file.")
                attempt, intent = commit.create_attempt(
                    task.id,
                    role="worker",
                    model="deepseek-flash",
                    prompt_version="worker-v2",
                    context_version="protocol-fixture-v1",
                    reservation=Reservation(tokens=4000, cost_micros=0),
                    intent_config={
                        "runtime_profile_id": PROFILE,
                        "model": "deepseek-flash",
                        "agent_config": agent.config.to_json(),
                        "message": message,
                        "provider_admission_fingerprint": guard.fingerprint,
                    },
                    input_hash=sha256_hex(message),
                )
                commit.claim_intent(intent.intent_id, owner="protocol-owner", lease_seconds=60)
                commit.record_agent_created(
                    intent.intent_id,
                    agent_id=agent.agent_id,
                    expected_turn_id=agent.turn_id_for(intent.input_id),
                )
                receipt = await agent.submit(message["content"], input_id=intent.input_id)
                commit.record_submitted(
                    intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
                )
                result = await agent.wait_turn(receipt.turn_id, timeout=5)
                records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
                grants = list(
                    store.connection.execute("SELECT * FROM provider_token_grants ORDER BY rowid")
                )
                assert tools.calls == []
                assert records[0].error_code == "provider_protocol_error"
                assert str(records[0].state) == "failed" and records[0].response_json is None
                assert records[0].handoff_attempt == 1
                assert records[0].estimator_digest == price.snapshot_digest
                assert records[0].usage_json["usage"]["total_tokens"] == 1100
                assert records[0].budget_charge.amount_micros == 2100
                assert grants[0]["state"] == "SETTLED"
                assert grants[0]["actual_tokens"] == 1100
                assert grants[0]["actual_cost_micros"] == 2100
                assert grants[0]["output_ceiling"] == 1000
                assert not commit.ledger.has_unknown_usage(attempt.id)

                if task_cost_micros == 10_000:
                    assert str(result.state) == "committed", result.error
                    assert result.public_output.content == "complete"
                    assert caps == [1000, 2000]
                    assert len(records) == len(grants) == 2
                    assert [str(record.state) for record in records] == ["failed", "succeeded"]
                    assert records[1].request_id != records[0].request_id
                    assert records[1].invocation_id != records[0].invocation_id
                    assert records[1].estimator_digest == price.snapshot_digest
                    assert grants[1]["state"] == "SETTLED"
                    assert grants[1]["prior_output_upper"] == 1000
                    assert grants[1]["output_ceiling"] == 2000
                    assert grants[1]["actual_tokens"] == 140
                    assert grants[1]["actual_cost_micros"] == 180
                    assert (
                        runtime.uow.read_provider_budget(records[0].run_id).committed_micros == 2280
                    )
                    facts = AgentBridge(runtime, unpriced=False).usage_facts(
                        agent_id=agent.agent_id
                    )
                    assert len(facts) == 2
                    assert [(fact.tokens, fact.cost_micros) for fact in facts] == [
                        (1100, 2100),
                        (140, 180),
                    ]
                    assert commit.import_usage(attempt.id, mission.id, facts) == 2
                    assert commit.import_usage(attempt.id, mission.id, facts) == 0
                    assert commit.ledger.usage_for(attempt.id) == (1240, 2280, False)
                    # Only the SDK turn has completed here. Its Orch Attempt is
                    # still active: do not manufacture subject-level completion.
                    assert commit.ledger.reservation(attempt.id)["state"] == "RESERVED"
                else:
                    # First request: 1,100 tokens / 2,100 micros. A doubled cap
                    # has a 3,100-token upper bound and costs 5,100 micros;
                    # the Task's 6,000-micro ceiling rejects 2,100 + 5,100.
                    assert task_cost_micros == 6000
                    assert str(result.state) == "failed", result.error
                    assert result.error["error_code"] == "provider_admission_denied"
                    denial = result.error["detail"]
                    assert denial["reason_code"] == "budget_exhausted"
                    assert denial["account_id"] == f"budget:{task.id}"
                    assert denial["dimension"] == "cost_micros"
                    assert denial["request_cost_micros"] == 5100
                    assert denial["requested"] > denial["remaining"]
                    assert caps == [1000]
                    assert len(records) == 2 and len(grants) == 1
                    assert str(records[1].state) == "claimed"
                    assert records[1].handoff_attempt == 0
                    assert records[1].usage_json.get("usage") is None
                    assert (
                        runtime.uow.read_provider_budget(records[0].run_id).committed_micros == 2100
                    )
                    assert commit.ledger.reservation(attempt.id)["state"] == "RESERVED"
                    facts = AgentBridge(runtime, unpriced=False).usage_facts(
                        agent_id=agent.agent_id
                    )
                    assert len(facts) == 1 and facts[0].tokens == 1100
                    assert facts[0].cost_micros == 2100
                    assert commit.import_usage(attempt.id, mission.id, facts) == 1
                    assert commit.import_usage(attempt.id, mission.id, facts) == 0
                    assert commit.ledger.usage_for(attempt.id) == (1100, 2100, False)

                # The settled grants and original failed invocation survive
                # reconciliation and a public same-input replay unchanged.
                before = [(r.invocation_id, r.request_id, r.version, r.usage_json) for r in records]
                grant_before = [dict(grant) for grant in grants]
                repeated = await agent.submit(message["content"], input_id=intent.input_id)
                assert repeated.turn_id == receipt.turn_id
                assert await agent.wait_turn(repeated.turn_id, timeout=5) == result
                await runtime.kernel.reconcile()
                guard.recover(runtime.uow)
                assert [
                    (r.invocation_id, r.request_id, r.version, r.usage_json)
                    for r in runtime.uow.list_provider_invocations(RunId(agent.run_id))
                ] == before
                assert [
                    dict(row)
                    for row in store.connection.execute(
                        "SELECT * FROM provider_token_grants ORDER BY rowid"
                    )
                ] == grant_before
                assert not commit.ledger.has_unknown_usage(attempt.id)
                assert tools.calls == []
                assert caps == ([1000, 2000] if task_cost_micros == 10_000 else [1000])
    finally:
        store.close()


def test_priced_truncated_tool_retries_with_new_identity_and_settles_both_grants(tmp_path):
    asyncio.run(_exercise_priced_length_recovery(tmp_path, task_cost_micros=10_000))


def test_priced_truncated_tool_second_admission_denied_by_task_budget(tmp_path):
    asyncio.run(_exercise_priced_length_recovery(tmp_path, task_cost_micros=6000))
