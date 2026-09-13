"""Genuine pre-context library; parent tests kill this process at SDK boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# This file is also invoked directly in an independent warm process.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_orchestrator.contracts import Budget
from agent_orchestrator.contracts.models import jsonable
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.execution.provider_invocations import provider_request_fingerprint

LONG = "deepseek-context-256k-v1"
LARGER = "deepseek-context-512k-v1"
SOURCE = "sources/original.md"
SOURCE_TEXT = "Original LC2 condition: the earlier identity must remain unchanged.\n"


class Counter:
    """Explicit deterministic test protocol, never an official model estimate."""

    fingerprint = "lc2-fixture-wire-v1"
    bound_protocol = "lc2-fixture-text-v1"
    requires_prior_output_reserve = False

    def estimate_input_tokens(self, request):
        return sum(len(str(message.content).encode()) for message in request.messages) + 1024


def orchestrator(root, provider, *, upgraded=False, slots=2, estimators=True, default=None):
    config = OrchestratorConfig(
        evidence_root=root,
        max_concurrent_model_calls=slots,
        sdk_lease_ttl_seconds=0.3,
        lease_seconds=0.6,
        turn_deadline_seconds=20,
    )
    # Old pool is genuinely context=None from its first open, not a modern pool
    # with a deleted sidecar. Neither old Agent nor old intent is rewritten.
    profiles = {"default": RuntimeProfile("default", provider, config.model)}
    if upgraded:
        for key, tokens in ((LONG, 262_144), (LARGER, 524_288)):
            profiles[key] = RuntimeProfile(
                key,
                provider,
                config.model,
                context_policy=ContextPolicy(
                    max_input_tokens=tokens,
                    output_reserve=32768,
                    max_tool_result_tokens=16384,
                    render_slack_tokens=0,
                ),
                default_max_output_tokens=8192,
                max_output_tokens_ceiling=32768,
            )
    options = {}
    if upgraded and estimators:
        options["provider_token_estimators"] = {"default": None, LONG: Counter(), LARGER: Counter()}
    return Orchestrator(
        config,
        profiles=profiles,
        routing=None if default is None else RoutingRules(default=default),
        **options,
    )


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 8)


async def planner(orch, key, *, profile=None, sources=False):
    if sources:
        receipt = orch.create_mission_with_sources(
            tenant_id="lc2",
            principal=Principal("lc2"),
            request={
                "goal": "Read the original source and write NOTES.md",
                "success_criteria": ["file:NOTES.md"],
                "domain": DOC_DOMAIN,
                "idempotency_key": key,
                "budget": {"max_tokens": 4_000_000, "max_attempts": 12},
            },
            sources=[{"path": SOURCE, "content": SOURCE_TEXT, "kind": "markdown"}],
        )
        mission = orch.store.get_mission(receipt["mission_id"])
    else:
        mission = await orch.submit_mission(
            MissionSpec(
                goal="Write NOTES.md",
                success_criteria=("file:NOTES.md",),
                tenant_id="lc2",
                idempotency_key=key,
                budget=Budget(max_tokens=4_000_000, max_attempts=12),
                runtime_profile_id=profile,
            )
        )
    orch.commit.begin_planning(mission.id)
    intent = await orch._create_planner_intent(mission.id, ordinal=1)
    return mission, intent


def records(orch, profile="default"):
    return list(
        orch.assembled.pool(profile).runtime.uow.database.connection.execute(
            "SELECT * FROM provider_invocations ORDER BY claimed_at, invocation_id"
        )
    )


def frozen(orch, intent_id):
    intent = orch.store.get_intent(intent_id)
    binding = orch.assembled.pool("default").runtime.uow.read_agent_binding(intent.agent_id)
    turn = orch.assembled.pool("default").runtime.uow.read_agent_turn(intent.expected_turn_id)
    return {
        "intent": {
            "config": dict(intent.config),
            "input_hash": intent.input_hash,
            "creation_key": intent.creation_key,
            "input_id": intent.input_id,
        },
        "binding": {
            "config_hash": binding.config_hash,
            "config_json": jsonable(binding.config_json),
        },
        "turn": {"input_hash": turn.input_hash, "input_id": turn.input_id},
        "requests": [
            {
                key: row[key]
                for key in (
                    "invocation_id",
                    "request_fingerprint",
                    "request_json",
                    "target_digest",
                    "estimator_digest",
                    "estimator_json",
                )
            }
            for row in records(orch)
        ],
        "sources": [dict(row) for row in orch.store.list_sources(intent.mission_id, True)],
    }


async def warm(mode, root, marker):
    provider = RoleScriptedProvider({"planner": ["unused"]}, gate=asyncio.Event())
    async with orchestrator(root, provider, upgraded=mode.startswith("compat-")) as orch:
        assert orch.assembled.pool("default").profile.context_snapshot() is None
        assert orch.assembled.pool("default").runtime.ports.provider_admission is None
        reached = asyncio.Event()
        prepared_wire = []
        if mode in {"pre", "compat-reserved", "compat-cas"}:
            admission = orch.assembled.pool("default").runtime.effective_provider_admission
            original_acquire = admission.acquire

            async def hold(**kwargs):
                prepared_wire.append(provider_request_fingerprint(kwargs["request"]))
                if mode == "compat-reserved":
                    await original_acquire(**kwargs)
                reached.set()
                await asyncio.Event().wait()

            if mode != "compat-cas":
                admission.acquire = hold
            else:
                # Exit the process after the real SDK CAS, before Orch commits.
                # The parent's OS kill tests use the other modes; this exact
                # transaction gap requires synchronous process exit, never await.
                import os
                from contextlib import contextmanager

                original_handoff = admission.handoff

                @contextmanager
                def crash(ticket, **kwargs):
                    with original_handoff(ticket, **kwargs):
                        yield
                        marker.write_text(json.dumps({"intent_id": intent.intent_id}))
                        os._exit(79)

                admission.handoff = crash
        mission, intent = await planner(orch, "original", sources=True)
        await orch._dispatch(intent)
        if mode in {"pre", "compat-reserved"}:
            await asyncio.wait_for(reached.wait(), 8)
        else:
            await until(lambda: provider.calls == 1)
        current = orch.store.get_intent(intent.intent_id)
        marker.write_text(
            json.dumps(
                {
                    "mission_id": mission.id,
                    "intent_id": intent.intent_id,
                    "calls": provider.calls,
                    "frozen": frozen(orch, intent.intent_id),
                    "wire_hash": (
                        prepared_wire[0]
                        if prepared_wire
                        else provider_request_fingerprint(provider.requests[0])
                    ),
                },
                ensure_ascii=False,
            )
        )
        assert current.config.get("provider_admission_fingerprint") is None
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(warm(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])))
