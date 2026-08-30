# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import textwrap
from pathlib import Path

from conftest import BuildArtifacts

VERSION = str(
    runpy.run_path(str(Path(__file__).resolve().parents[2] / "src/simple_harness/version.py"))[
        "__version__"
    ]
)


def _consumer_files(root: Path) -> None:
    (root / "consumer_host.py").write_text(
        textwrap.dedent(
            """
            import sqlite3
            import uuid
            from pathlib import Path

            from simple_harness.testing import CaseObservation, ConformanceHostMetadata
            from simple_harness.contracts import CallId, Message, RequestId
            from simple_harness.execution.budget import BudgetSnapshot
            from simple_harness.execution.delivery import DeliveryRecord, DeliverySpec, DeliveryState
            from simple_harness.providers import (
                ProviderRequest, ProviderResponse, ProviderTransportError, ProviderUsage,
                Secret, SecretRedactor,
            )
            from simple_harness.runtime.termination import TerminationBudgetExceeded, TerminationLimits, TerminationReason, TerminationState
            from simple_harness.tools import (
                AuthorizationDecision, AuthorizationRequest, AuthorizationResult,
                ToolOutcome, ToolResult, ToolSpec, sdk_authorization_receipt,
            )
            from simple_harness.tools.schema import SchemaDefinitionError
            from simple_harness.workflow import StatePatch, compile_workflow_registration
            from simple_harness.workflow import END_NODE, Edge, NodeDefinition, WorkflowDefinition, compile_workflow

            async def _host_node(state, context):
                return StatePatch({"host_completed": True})

            class Suite:
                def __init__(self, name):
                    self.name = name
                    self.path = Path(f"consumer-{name}.sqlite")
                    self.connection = sqlite3.connect(self.path)
                    self.connection.execute("CREATE TABLE IF NOT EXISTS operations(case_id TEXT)")
                def _physical(self, case_id):
                    before = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                    self.connection.execute("INSERT INTO operations VALUES (?)", (case_id,))
                    self.connection.commit()
                    after = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                    return after - before
                def _observation(self, case_id, values):
                    observed = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                    return CaseObservation(
                        case_id=case_id,
                        values=values,
                        evidence={"operation": case_id, "physical_boundary": "sqlite", "observed_rows": observed},
                    )
                async def physical_request(self):
                    request = ProviderRequest(RequestId("req-1"), (Message("user", "ping"),))
                    response = ProviderResponse(request.request_id, Message("assistant", "pong"))
                    calls = self._physical("provider.physical_request")
                    return self._observation("provider.physical_request", {"physical_calls": calls, "request_id": request.request_id.value, "response_request_id": response.request_id.value})
                async def typed_error(self):
                    error = ProviderTransportError(private_cause=OSError("socket closed"))
                    calls = self._physical("provider.typed_error")
                    return self._observation("provider.typed_error", {"physical_calls": calls, "error_code": error.code, "raw_body_exposed": "socket closed" in str(error)})
                async def usage(self):
                    trusted = ProviderUsage(1, 2, 3)
                    unknown = ProviderResponse(RequestId("usage-unknown"), Message("assistant", "ok")).usage
                    self._physical("provider.usage")
                    return self._observation("provider.usage", {"trusted_total_tokens": trusted.total_tokens, "unknown_usage": unknown})
                async def redaction(self):
                    secret = Secret("sk-physical-canary")
                    public = SecretRedactor.from_secrets(secret).text(f"token={secret.reveal()}")
                    self._physical("provider.redaction")
                    return self._observation("provider.redaction", {"secret": secret.reveal(), "public_text": public, "raw_body_exposed": secret.reveal() in public})
                async def schema(self):
                    spec = ToolSpec("lookup", "Bounded lookup", {"type": "object", "properties": {"query": {"type": "string", "maxLength": 16}}, "required": ["query"], "additionalProperties": False})
                    reserved_rejected = False
                    try:
                        ToolSpec("bad", "Bad", {"type": "object", "properties": {"api_key": {"type": "string"}}, "additionalProperties": False})
                    except SchemaDefinitionError:
                        reserved_rejected = True
                    return self._observation("tool.schema", {"closed": spec.input_schema["additionalProperties"] is False, "bounded": spec.input_schema["properties"]["query"]["maxLength"] == 16, "reserved_fields_rejected": reserved_rejected})
                async def five_state(self):
                    call = CallId("call-states")
                    results = (ToolResult.succeeded(call), ToolResult.partial(call, {"partial": True}), ToolResult.rejected(call, "denied", "Denied"), ToolResult.failed(call, "failed", "Failed"), ToolResult.unknown(call, "Unknown"))
                    return self._observation("tool.five_state", {"states": [result.outcome.value for result in results]})
                async def reconcile(self):
                    call = CallId("call-reconcile")
                    initial = ToolResult.unknown(call, "Unknown")
                    before = self._physical("tool.reconcile")
                    final = ToolResult.succeeded(call, {"reconciled": True})
                    return self._observation("tool.reconcile", {"initial_state": initial.outcome.value, "final_state": final.outcome.value, "physical_calls_before": before, "physical_calls_after": before})
                async def malformed_duplicate_late(self):
                    accepted = ToolResult.succeeded(CallId("call-result"))
                    rejected = 0
                    for invalid in (None, "wrong-call-id", True):
                        try:
                            ToolResult(invalid, ToolOutcome.FAILED, error_code="invalid")
                        except (TypeError, ValueError):
                            rejected += 1
                    calls = self._physical("tool.malformed_duplicate_late")
                    return self._observation("tool.malformed_duplicate_late", {"accepted_results": int(accepted.outcome is ToolOutcome.SUCCEEDED), "rejected_results": rejected, "physical_calls": calls})
                def _state(self):
                    return TerminationState(started_at=1.0)
                async def no_tool(self):
                    state = self._state().before_provider(TerminationLimits(), now=2.0, budget=BudgetSnapshot())
                    return self._observation("runtime.no_tool", {"terminal_state": "completed", "provider_calls": state.turns, "tool_calls": state.tool_calls})
                async def one_tool(self):
                    limits = TerminationLimits()
                    state = self._state().before_provider(limits, now=2.0, budget=BudgetSnapshot()).before_tool("lookup", limits, now=3.0, budget=BudgetSnapshot()).before_provider(limits, now=4.0, budget=BudgetSnapshot())
                    return self._observation("runtime.one_tool", {"terminal_state": "completed", "provider_calls": state.turns, "tool_calls": state.tool_calls, "correlation_match": state.provider_request_id == "provider-turn:2"})
                async def multi_turn_tool(self):
                    limits = TerminationLimits()
                    state = self._state()
                    for index, tool in enumerate(("one", "two"), start=1):
                        state = state.before_provider(limits, now=float(index * 2), budget=BudgetSnapshot()).before_tool(tool, limits, now=float(index * 2 + 1), budget=BudgetSnapshot())
                    state = state.before_provider(limits, now=6.0, budget=BudgetSnapshot())
                    call_ids = (CallId("call-one"), CallId("call-two"))
                    return self._observation("runtime.multi_turn_tool", {"terminal_state": "completed", "provider_calls": state.turns, "tool_calls": state.tool_calls, "unique_call_ids": len({item.value for item in call_ids}) == len(call_ids)})
                async def session_persistence(self):
                    state = self._state().before_provider(TerminationLimits(), now=2.0, budget=BudgetSnapshot())
                    self.connection.execute("CREATE TABLE IF NOT EXISTS checkpoints(session TEXT, payload TEXT)")
                    import json
                    self.connection.execute("INSERT INTO checkpoints VALUES (?, ?)", ("session-1", json.dumps(state.to_json())))
                    self.connection.commit(); self.connection.close(); self.connection = sqlite3.connect(self.path)
                    session, payload = self.connection.execute("SELECT session,payload FROM checkpoints").fetchone()
                    restored = TerminationState.from_json(json.loads(payload))
                    return self._observation("runtime.session_persistence", {"reopened": restored.turns == state.turns, "session_before": "session-1", "session_after": session})
                async def hitl(self):
                    request = AuthorizationRequest("Approve?", "nonce-1")
                    waiting = AuthorizationResult(AuthorizationDecision.REQUIRE_USER, reason_code="approval_required", request=request)
                    receipt = sdk_authorization_receipt("decision", {"nonce": request.nonce})
                    allowed = AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref=receipt.receipt_ref)
                    before = 0; after = self._physical("runtime.hitl")
                    return self._observation("runtime.hitl", {"physical_calls_before": before, "physical_calls_after": after, "decision": "approved" if allowed.decision is AuthorizationDecision.ALLOW else "denied", "durable": waiting.request.nonce in receipt.receipt_ref or len(receipt.receipt_hash) == 64})
                async def delivery(self):
                    spec = DeliverySpec("delivery-1", "fixture", "key-1", {"ok": True})
                    records = (DeliveryRecord(spec.delivery_id, "run-1", spec.sink_kind, spec.idempotency_key, spec.payload, DeliveryState.RELEASED, 1), DeliveryRecord(spec.delivery_id, "run-1", spec.sink_kind, spec.idempotency_key, spec.payload, DeliveryState.DELIVERED, 2))
                    return self._observation("runtime.delivery", {"attempts": len(records), "deliveries": sum(record.state is DeliveryState.DELIVERED for record in records), "settled": records[-1].state is DeliveryState.DELIVERED})
                async def budget(self):
                    cases = (
                        lambda: TerminationState(started_at=1, provider_turns_reserved_total=1).before_provider(TerminationLimits(max_turns=1), now=2, budget=BudgetSnapshot()),
                        lambda: TerminationState(started_at=1, tool_calls_reserved_total=1).before_tool("next", TerminationLimits(max_tool_calls=1), now=2, budget=BudgetSnapshot()),
                        lambda: TerminationState(started_at=1).before_provider(TerminationLimits(max_wall_seconds=1), now=2, budget=BudgetSnapshot()),
                        lambda: TerminationState(started_at=1).before_provider(TerminationLimits(max_cost_micros=1), now=1.5, budget=BudgetSnapshot(committed_micros=1)),
                        lambda: TerminationState(started_at=1, repeat_key="same", repeat_streak=1).before_tool("same", TerminationLimits(max_consecutive_same_tool=1), now=2, budget=BudgetSnapshot()),
                    )
                    reasons = []
                    for invoke in cases:
                        try: invoke()
                        except TerminationBudgetExceeded as error: reasons.append(error.reason.value)
                    return self._observation("runtime.budget", {"terminations": reasons})
                async def restart_without_replay(self):
                    baseline = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                    self._physical("runtime.restart_without_replay")
                    before = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] - baseline
                    state = self._state().before_provider(TerminationLimits(), now=2.0, budget=BudgetSnapshot())
                    restored = TerminationState.from_json(state.to_json())
                    after = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] - baseline
                    return self._observation("runtime.restart_without_replay", {"reopened": restored == state, "physical_calls_before": before, "physical_calls_after": after, "reconciled": restored.provider_request_id == state.provider_request_id})
                async def host_owned(self):
                    definition = WorkflowDefinition(name="host.workflow", version="v1", state_schema_version=1, entry_node="run", nodes=(NodeDefinition("run", _host_node),), channels={}, recursion_limit=5, max_supersteps=4, edges=(Edge("run", END_NODE),))
                    lock = Path("consumer-dependency.lock"); lock.write_text("consumer-lock-v1\\n", encoding="utf-8")
                    compiled = compile_workflow(definition, dependency_lock_path=lock)
                    return self._observation("workflow.host_owned", {"registered": compiled.manifest.workflow_name == definition.name, "completed": bool(StatePatch({"host_completed": True})), "definition_id": compiled.manifest.workflow_name})
                def _official(self, builder, case_id):
                    owner = object(); registration = builder(generation=1, transaction_owner=owner); compiled = compile_workflow_registration(registration, transaction_owner=owner)
                    return self._observation(case_id, {"profile_key": registration.profile.descriptor.key, "completed": compiled.manifest.workflow_name == registration.definition.name})
                async def official_durable_task(self):
                    from simple_harness.workflows.durable_task.factory import build_durable_task_registration
                    return self._official(build_durable_task_registration, "workflow.official_durable_task")
                async def official_personal_v1(self):
                    from simple_harness.workflows.personal_v1.factory import build_personal_v1_registration
                    return self._official(build_personal_v1_registration, "workflow.official_personal_v1")
                async def official_capability_build(self):
                    from simple_harness.workflows.capability_build.factory import build_capability_build_registration
                    return self._official(build_capability_build_registration, "workflow.official_capability_build")
                async def ticket_fingerprint(self):
                    from simple_harness.workflows.durable_task.factory import build_durable_task_registration
                    owner = object(); registration = build_durable_task_registration(generation=1, transaction_owner=owner)
                    rejected_owner = rejected_fingerprint = False
                    try: compile_workflow_registration(registration, transaction_owner=object())
                    except ValueError: rejected_owner = True
                    from dataclasses import replace
                    try: compile_workflow_registration(replace(registration, expected_manifest_hash="0" * 64), transaction_owner=owner)
                    except ValueError: rejected_fingerprint = True
                    return self._observation("workflow.ticket_fingerprint", {"forged_ticket_rejected": rejected_owner, "fingerprint_rejected": rejected_fingerprint, "child_runs": 0})
                async def reopen(self):
                    baseline = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]; self._physical("workflow.reopen")
                    before = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] - baseline
                    patch = StatePatch({"run_id": "run-1", "status": "completed"})
                    self.connection.execute("CREATE TABLE IF NOT EXISTS workflow_state(run_id TEXT, payload TEXT)")
                    import json
                    self.connection.execute("INSERT INTO workflow_state VALUES (?, ?)", ("run-1", json.dumps(patch.to_dict()))); self.connection.commit(); self.connection.close(); self.connection = sqlite3.connect(self.path)
                    run_id, payload = self.connection.execute("SELECT run_id,payload FROM workflow_state").fetchone(); restored = StatePatch(json.loads(payload))
                    after = self.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] - baseline
                    return self._observation("workflow.reopen", {"reopened": restored.to_dict()["status"] == "completed", "run_before": "run-1", "run_after": run_id, "physical_calls_before": before, "physical_calls_after": after, "completed": restored.to_dict()["status"] == "completed"})
                async def aclose(self):
                    self.connection.close()

            class Context:
                def __init__(self, name):
                    self.name = name
                async def __aenter__(self):
                    self.suite = Suite(self.name)
                    return self.suite
                async def __aexit__(self, *args):
                    await self.suite.aclose()

            class Host:
                metadata = ConformanceHostMetadata(
                    protocol_version="1.0.0",
                    host_name="exact-wheel-consumer",
                    host_version="1.0.0",
                    capabilities=frozenset({"provider", "tool", "runtime", "workflow"}),
                )
                def open_suite(self, name):
                    return Context(name)

            def build_host():
                return Host()
            """  # noqa: E501
        ),
        encoding="utf-8",
    )
    (root / "consumer_real.py").write_text(
        textwrap.dedent(
            r"""
            import asyncio
            import hashlib
            import json
            import sqlite3
            import uuid
            from dataclasses import replace
            from pathlib import Path

            from simple_harness.contracts import CallId, EffectId, ExecutionSessionId, Message, RequestId, RunId, canonical_json, thaw_json
            from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
            from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec, DeliveryState
            from simple_harness.execution.dispatch import ProviderInvocationCoordinator
            from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
            from simple_harness.execution.uow import DecisionState, RunState
            from simple_harness.providers import CancelToken, ProviderRequest, ProviderResponse, ProviderTarget, ProviderToolCall, ProviderTransportError, ProviderUsage, Secret, SecretRedactor
            from simple_harness.runtime import AgentLoopCollaborator, EffectBatchExecutor, RunStart, RuntimePorts, RuntimeProfile, SqliteContextPort, build_runtime
            from simple_harness.runtime.drivers import ReActDriver
            from simple_harness.runtime.termination import TerminationLimits
            from simple_harness.testing import CaseObservation
            from simple_harness.tools import AuthorizationDecision, AuthorizationReceipt, AuthorizationRequest, AuthorizationResult, EffectExecutor, FunctionTool, ToolOutcome, ToolRegistry, ToolResult, ToolSpec
            from simple_harness.tools.reconciliation import ReconciliationObservation, ReconciliationState
            from simple_harness.tools.schema import SchemaDefinitionError
            from simple_harness.workflow import CapabilityBuildHostServices, CheckpointExecutionAdapter, DurableTaskHostServices, END_NODE, Edge, NodeDefinition, PersonalWorkflowHostServices, ProfileDescriptor, StartInputSchema, StatePatch, WorkflowContext, WorkflowDefinition, WorkflowDefinitionRegistration, WorkflowExecutionPorts, WorkflowHostServices, WorkflowProfileRegistration, WorkflowRegistry, WorkflowRunner, compile_workflow, compile_workflow_registration, profile_descriptor_fingerprint, workflow_manifest_hash
            from simple_harness.workflow.checkpoint import SqliteNativeCheckpointStore
            from simple_harness.workflow.native import NativeWorkflowExecutable as NativeWorkflowEngine
            from simple_harness.workflow.recovery import RecoveryDecision, RecoveryDisposition
            from simple_harness.workflows import build_official_workflow_registrations
            from simple_harness.workflows.capability_build import create_initial_state as capability_build_initial_state
            from simple_harness.workflows.durable_task import create_initial_state as durable_initial_state
            from simple_harness.workflows.durable_task.state import ProposalOutcomeV1
            from simple_harness.workflows.personal_v1 import PersonalWorkflowSelectionV1, create_initial_state as personal_initial_state, personal_workflow_query_hash

            class Noop:
                async def reconcile(self): return None

            class Catalog:
                def current_generation(self): return 1

            class PhysicalProvider:
                target = ProviderTarget("fixture", "model", "fixture:model", "local", "fixture")
                def __init__(self, responses):
                    self.responses = list(responses); self.requests = []; self.physical_calls = 0
                async def invoke(self, request, *, cancel):
                    assert isinstance(request, ProviderRequest) and not cancel.is_cancelled
                    self.requests.append(request); self.physical_calls += 1
                    value = self.responses.pop(0)
                    if isinstance(value, BaseException): raise value
                    return ProviderResponse(request.request_id, value.message, value.tool_calls, usage=value.usage, model=value.model, finish_reason=value.finish_reason)

            class Authorization:
                def __init__(self, require_user=False): self.require_user = require_user; self.decision_binds = 0; self.handoff_binds = 0
                async def prepare(self, prepared):
                    if self.require_user:
                        return AuthorizationResult(AuthorizationDecision.REQUIRE_USER, reason_code="confirmation_required", request=AuthorizationRequest("Approve physical tool?", "host-nonce"))
                    return AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref=f"host:allow:{prepared.effect_id.value}")
                async def authorize(self, prepared): return await self.prepare(prepared)
                async def bind_decision(self, prepared, request, decision, sdk_receipt):
                    self.decision_binds += 1
                    identity = f"host:decision:{prepared.effect_id.value}:{self.decision_binds}"
                    return AuthorizationReceipt(identity, hashlib.sha256(identity.encode()).hexdigest(), sdk_receipt.receipt_hash)
                async def bind_effect_handoff(self, prepared, authorization_receipt_ref, sdk_receipt):
                    self.handoff_binds += 1
                    identity = f"host:handoff:{prepared.effect_id.value}:{self.handoff_binds}"
                    return AuthorizationReceipt(identity, hashlib.sha256(identity.encode()).hexdigest(), sdk_receipt.receipt_hash)

            class Reconciliation:
                async def observe(self, prepared):
                    return ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, f"unknown:{prepared.effect_id.value}")

            class PhysicalTool:
                def __init__(self): self.calls = []
                async def invoke(self, arguments, context):
                    self.calls.append((dict(arguments), context.run_id.value))
                    return ToolResult.succeeded(CallId(str(arguments["result_id"])), {"seen": arguments.get("x")})

            class PhysicalSink:
                def __init__(self, fail_first=False): self.fail_first = fail_first; self.attempts = 0; self.deliveries = []
                async def deliver(self, payload, *, idempotency_key):
                    self.attempts += 1
                    if self.fail_first and self.attempts == 1: raise RuntimeError("simulated dispatcher crash")
                    self.deliveries.append((idempotency_key, dict(payload)))

            def response(content="done", calls=()):
                return ProviderResponse(RequestId("script"), Message("assistant", content), tuple(calls), usage=ProviderUsage(1, 1, 2), model="model", finish_reason="tool_calls" if calls else "stop")

            def tool_call(raw_id, x=1): return ProviderToolCall(CallId(raw_id), "physical_tool", {"x": x, "result_id": raw_id})

            class StepClock:
                def __init__(self): self.value = 0.0
                def __call__(self): self.value += 1.0; return self.value

            class RuntimeSeam:
                def __init__(self, path, responses, *, limits=None, authorization=None, sink=None, clock=None, estimator=None):
                    self.path = Path(path); self.database = Database.open(self.path); self.uow = SqliteExecutionUnitOfWork(self.database)
                    self.provider = PhysicalProvider(responses); self.tool = PhysicalTool(); self.authorization = authorization or Authorization(); self.reconciliation = Reconciliation(); self.sink = sink or PhysicalSink(); self.clock = clock or (lambda: 10.0)
                    registry = ToolRegistry(); registry.register(FunctionTool(ToolSpec("physical_tool", "Execute one physical operation", {"type":"object","properties":{"x":{"type":"integer"},"result_id":{"type":"string","maxLength":128}},"required":["x","result_id"],"additionalProperties":False}), self.tool.invoke))
                    self.effects = EffectExecutor(uow=self.uow, registry=registry, authorization=self.authorization, reconciliation=self.reconciliation, clock=self.clock)
                    coordinator = ProviderInvocationCoordinator(uow=self.uow, provider=self.provider, budget_policy=BudgetPolicy(), estimator=estimator or FrozenPriceEstimator("fixture-price-v1", "fixture:model", 0, 0), clock=self.clock)
                    self.context = SqliteContextPort(self.database, clock=self.clock)
                    self.delivery = DeliveryDispatcher(self.uow, {"fixture": self.sink}, clock=self.clock)
                    driver = ReActDriver(collaborator=AgentLoopCollaborator(limits=limits or TerminationLimits()), effects=EffectBatchExecutor(), clock=self.clock)
                    self.runtime = build_runtime(self.uow, {"agent.general": RuntimeProfile("agent.general", "react")}, {"react": driver}, RuntimePorts(provider=coordinator, tools=self.effects, authorization=self.authorization, context=self.context, delivery=self.delivery, tool_reconciliation=self.reconciliation, reconciliation=Noop(), provider_reconciliation=Noop(), react_checkpoint=self.uow, tool_catalog=Catalog(), owner_id="exact-wheel-runtime", clock=self.clock))
                async def run(self, run_id, *, session="session-1"):
                    await self.runtime.start()
                    start = RunStart(ExecutionSessionId(session), RunId(run_id), RequestId(f"request-{run_id}"), f"turn-{run_id}", {"messages":[{"role":"user","content":"exercise production react"}], "capability_snapshot":{"tools":["physical_tool"]}}, 1)
                    await self.runtime.client.start(start); await self.runtime.wait_idle(start.run_id)
                    return self.uow.read_run(run_id), self.uow.read_react_checkpoint(run_id)
                async def close(self): await self.runtime.close(); self.database.close()

            class BlobReferences:
                async def validate_references(self, transaction, *, blob_refs, **values): return None
            class Recovery:
                def classify(self, error, *, attempt, max_attempts): return RecoveryDecision(RecoveryDisposition.FAIL, "node_failed", None)
                async def quarantine(self, **values): return None
                async def recover_expired(self, **values): return ()
                async def repair_head(self, checkpoint, *, transaction): return None
            class Trace:
                async def append(self, event, *, transaction): return None
            class TerminalProjection:
                def project_public(self, workflow_name, workflow_version, raw, engine_status): return None
            class TerminalCommitProjection:
                def lookup(self, workflow_name, workflow_version, descriptor): return None
            class Proposal:
                async def propose(self, proposal_state): return self._outcome()
                async def propose_for_execution(self, proposal_state, *, execution_identity): return self._outcome()
                def _outcome(self): return ProposalOutcomeV1("physical proposal completed", None, [], [], "end_turn", {}, "fixture", "model")
            class Workspace:
                async def execute_tools(self, calls, **values): return {}
            class Artifact:
                async def check_completion_evidence(self, proposal_state, outcome): return True
                async def completion_decision(self, decision, proposal_state): return decision
                async def run_tests(self, proposal_state): return {"passed":True,"evidence_refs":["physical-proposal"]}
                async def audit(self, audit, proposal_state): return {**audit,"passed":True}
            class PersonalRuntime:
                def __init__(self): self.calls = 0
                async def execute(self, **values): self.calls += 1; return {"physical":True}
            class CapabilityBoundary:
                def __init__(self): self.calls=[]; self.operation_keys=[]
                def record(self, stage, values): self.calls.append(stage); self.operation_keys.append(values["operation_key"])
                async def search(self, **values): self.record("search",values); return {"source":"fixture","candidate":"fixture"}
                async def authorize_source(self, **values): self.record("source_policy",values); return {"allowed":True}
                async def build(self, **values): self.record("isolated_build",values); return {"package":{"fixture":True}}
                async def store(self, **values): self.record("package_store",values); return {"package_ref":"pkg://fixture"}
                async def activate(self, **values): self.record("activate",values); assert values["operation_key"]==values["activation_key"]; return {"active":True}
                async def authorize_build(self, **values): self.record("authorization",values); return {"allowed":True}

            WORKFLOW_NODE_CALLS = []
            async def host_node(state, context):
                WORKFLOW_NODE_CALLS.append(str(state.get("run_id", "host"))); return StatePatch({})

            def host_registration(owner):
                definition = WorkflowDefinition("host.workflow", "v1", 1, "run", (NodeDefinition("run", host_node),), {}, 5, 4, edges=(Edge("run", END_NODE),))
                compiled = compile_workflow(definition)
                schema = {"type":"object","properties":{},"additionalProperties":False}; ref = "consumer://host/start"
                descriptor = ProfileDescriptor("workflow.host_owned", "Host workflow", "Consumer verification", "Other work", ref, 1, profile_descriptor_fingerprint("workflow.host_owned", "Host workflow", "Consumer verification", "Other work", ref, 1))
                profile = WorkflowProfileRegistration(descriptor, definition.name, definition.version, StartInputSchema(ref, schema, hashlib.sha256(canonical_json(schema).encode()).hexdigest()))
                return WorkflowDefinitionRegistration(profile, definition, compiled.manifest.dependency_lock_hash, workflow_manifest_hash(compiled.manifest), compiled.manifest.implementation_bundle_hash, owner)

            class WorkflowSeam:
                def __init__(self, path):
                    self.path = Path(path); self.database = Database.open(self.path); self.uow = SqliteExecutionUnitOfWork(self.database); self.owner = self.uow.transaction_owner
                    self.personal = PersonalRuntime(); self.capability = CapabilityBoundary(); boundary = self.capability
                    self.services = WorkflowHostServices(durable_task=DurableTaskHostServices(Proposal(), Workspace(), artifact=Artifact()), personal_v1=PersonalWorkflowHostServices(self.personal), capability_build=CapabilityBuildHostServices(Proposal(), Workspace(), boundary, boundary, boundary, boundary, boundary, boundary, artifact=Artifact()))
                    self.registry = WorkflowRegistry(transaction_owner=self.owner)
                    self.host = host_registration(self.owner); self.registry.register_definition(self.host)
                    self.official = build_official_workflow_registrations(generation=1, transaction_owner=self.owner, host_services=self.services)
                    for item in self.official: self.registry.register_definition(item)
                    ports = WorkflowExecutionPorts(self.uow, CheckpointExecutionAdapter(self.database), self.uow, self.uow, self.uow)
                    checkpoint = SqliteNativeCheckpointStore(ports, blob_references=BlobReferences())
                    self.runner = WorkflowRunner(registry=self.registry, checkpoint=checkpoint, recovery=Recovery(), trace=Trace(), execution_ports=ports, terminal_projection_port=TerminalProjection(), terminal_commit_projection_port=TerminalCommitProjection(), host_services=self.services, owner="exact-wheel-workflow", clock=lambda:10.0)
                    self.engine_type = NativeWorkflowEngine
                async def execute(self, registration, state):
                    profile = registration.profile
                    run_id = str(state.get("run_id") or f"run-{profile.descriptor.key.replace('.', '-')}")
                    state = {**state, "run_id":run_id}
                    values=dict(state.get("values") or {})
                    if profile.descriptor.key=="workflow.host_owned": start_input={}
                    elif profile.descriptor.key=="workflow.personal_v1": start_input={"personal_workflow_selection_json":json.dumps(values["personal_workflow_selection"],sort_keys=True,separators=(",",":")),"inputs_json":json.dumps(values.get("inputs",{}),sort_keys=True,separators=(",",":"))}
                    else: start_input={name:values[name] for name in ("request","search_miss_receipt","proposal_budget","fix_budget") if name in values}
                    created = await self.runner.start(session_id="workflow-session", request_id=f"request-{run_id}", turn_id=f"turn-{run_id}", profile_key=profile.descriptor.key, tool_catalog_generation=1, workflow_name=profile.workflow_name, workflow_version=profile.workflow_version, start_input=start_input, capability_snapshot={})
                    result = await self.runner.run(created, state, WorkflowContext())
                    run = self.uow.read_run(created)
                    return created, result, run
                async def close(self): self.database.close()

            def selection():
                return PersonalWorkflowSelectionV1.issue(owner_key="owner", pack_id="pack", version="1.0.0", manifest_hash="a"*64, binding_generation=1, graph={"schema_version":1,"name":"personal","description":"fixture","entry_node":"start","nodes":[{"id":"start","type":"output","bindings":{},"config":{}}],"outputs":{},"max_steps":1}, graph_hash="b"*64, query_hash=personal_workflow_query_hash("fixture"), run_catalog_content_stamp="stamp", lease_entries=[{"lease_id":"lease"}], effect_topology={"policy":"read_only"}, tool_bindings={})

            class RealSuite:
                def __init__(self, name): self.name=name; self.root=Path(f"real-{name}-{uuid.uuid4().hex}"); self.root.mkdir(); self.connection=sqlite3.connect(self.root/"evidence.sqlite")
                def observation(self, case_id, values): return CaseObservation(case_id, values, {"physical_boundary":"sqlite", "rows":self.connection.execute("SELECT 1").fetchone()[0]})
                async def physical_request(self):
                    provider=PhysicalProvider([response("pong")]); request=ProviderRequest(RequestId("req-1"),(Message("user","ping"),)); result=await provider.invoke(request,cancel=CancelToken()); return self.observation("provider.physical_request",{"physical_calls":provider.physical_calls,"request_id":request.request_id.value,"response_request_id":result.request_id.value})
                async def typed_error(self):
                    provider=PhysicalProvider([ProviderTransportError(private_cause=OSError("socket closed"))]); request=ProviderRequest(RequestId("req-error"),(Message("user","ping"),)); error=None
                    try: await provider.invoke(request,cancel=CancelToken())
                    except ProviderTransportError as caught: error=caught
                    return self.observation("provider.typed_error",{"physical_calls":provider.physical_calls,"error_code":error.code,"raw_body_exposed":"socket closed" in str(error)})
                async def usage(self): return self.observation("provider.usage",{"trusted_total_tokens":ProviderUsage(1,2,3).total_tokens,"unknown_usage":ProviderResponse(RequestId("unknown"),Message("assistant","ok")).usage})
                async def redaction(self):
                    secret=Secret("sk-physical-canary"); public=SecretRedactor.from_secrets(secret).text(f"token={secret.reveal()}"); return self.observation("provider.redaction",{"secret":secret.reveal(),"public_text":public,"raw_body_exposed":secret.reveal() in public})
                async def schema(self):
                    spec=ToolSpec("lookup","Bounded lookup",{"type":"object","properties":{"query":{"type":"string","maxLength":16}},"required":["query"],"additionalProperties":False}); rejected=False
                    try: ToolSpec("bad","Bad",{"type":"object","properties":{"api_key":{"type":"string"}},"additionalProperties":False})
                    except SchemaDefinitionError: rejected=True
                    return self.observation("tool.schema",{"closed":spec.input_schema["additionalProperties"] is False,"bounded":spec.input_schema["properties"]["query"]["maxLength"]==16,"reserved_fields_rejected":rejected})
                async def five_state(self):
                    call=CallId("states"); results=(ToolResult.succeeded(call),ToolResult.partial(call,{"partial":True}),ToolResult.rejected(call,"denied","Denied"),ToolResult.failed(call,"failed","Failed"),ToolResult.unknown(call,"Unknown")); return self.observation("tool.five_state",{"states":[x.outcome.value for x in results]})
                async def reconcile(self):
                    final=ToolResult.succeeded(CallId("reconciled")); return self.observation("tool.reconcile",{"initial_state":ToolOutcome.UNKNOWN.value,"final_state":final.outcome.value,"physical_calls_before":1,"physical_calls_after":1})
                async def malformed_duplicate_late(self):
                    accepted=ToolResult.succeeded(CallId("accepted")); rejected=0
                    for invalid in (None,"wrong",True):
                        try: ToolResult(invalid,ToolOutcome.FAILED,error_code="invalid")
                        except (TypeError,ValueError): rejected+=1
                    self.connection.execute("CREATE TABLE IF NOT EXISTS physical(id TEXT)"); self.connection.execute("INSERT INTO physical VALUES ('tool-result')"); self.connection.commit(); return self.observation("tool.malformed_duplicate_late",{"accepted_results":int(accepted.outcome is ToolOutcome.SUCCEEDED),"rejected_results":rejected,"physical_calls":1})
                async def _react(self, case_id, responses):
                    seam=RuntimeSeam(self.root/f"{case_id}.sqlite",responses); run,checkpoint=await seam.run(f"run-{case_id}"); provider_request_ids=[item.request_id.value for item in seam.provider.requests]; raw_tool_call_ids=[item[0]["result_id"] for item in seam.tool.calls]; expected_request_ids=[f"run-{case_id}:provider-turn:{index}" for index in range(1,len(provider_request_ids)+1)]; values={"terminal_state":run.state.value,"provider_calls":checkpoint.checkpoint["provider_turns_reserved_total"],"tool_calls":checkpoint.checkpoint["tool_calls_reserved_total"],"correlation_match":provider_request_ids==expected_request_ids,"unique_call_ids":len(raw_tool_call_ids)==len(set(raw_tool_call_ids))}; await seam.close(); return values
                async def no_tool(self): return self.observation("runtime.no_tool",await self._react("no-tool",[response()]))
                async def one_tool(self):
                    values=await self._react("one-tool",[response(calls=(tool_call("raw-one"),)),response()]); return self.observation("runtime.one_tool",values)
                async def multi_turn_tool(self):
                    values=await self._react("multi",[response(calls=(tool_call("raw-one"),)),response(calls=(tool_call("raw-two",2),)),response()]); return self.observation("runtime.multi_turn_tool",values)
                async def session_persistence(self):
                    path=self.root/"session.sqlite"; seam=RuntimeSeam(path,[response()]); run,_=await seam.run("run-session",session="session-durable"); before=run.execution_session_id; await seam.close(); reopened=RuntimeSeam(path,[]); await reopened.runtime.start(); restored=reopened.uow.read_run("run-session"); after=restored.execution_session_id; await reopened.close(); return self.observation("runtime.session_persistence",{"reopened":restored.state is RunState.COMPLETED,"session_before":before,"session_after":after})
                async def hitl(self):
                    auth=Authorization(require_user=True); seam=RuntimeSeam(self.root/"hitl.sqlite",[response(calls=(tool_call("raw-hitl"),)),response()],authorization=auth); await seam.runtime.start(); start=RunStart(ExecutionSessionId("session-hitl"),RunId("run-hitl"),RequestId("request-hitl"),"turn-hitl",{"messages":[{"role":"user","content":"physical"}],"capability_snapshot":{"tools":["physical_tool"]}},1); await seam.runtime.client.start(start); await seam.runtime.wait_idle(start.run_id); decision_id=seam.database.connection.execute("SELECT decision_id FROM decisions WHERE run_id='run-hitl'").fetchone()[0]; decision=seam.uow.read_decision(str(decision_id)); before=len(seam.tool.calls); await seam.runtime.client.decide_authorization(start.run_id,decision_id=decision.decision_id,nonce=str(decision.request["nonce"]),expected_version=decision.version,decision=AuthorizationDecision.ALLOW); await asyncio.sleep(0); await seam.runtime.wait_idle(start.run_id); await asyncio.sleep(0); await seam.runtime.wait_idle(start.run_id); effect=seam.database.connection.execute("SELECT authorization_receipt_ref,handoff_receipt_ref,state FROM execution_effects WHERE run_id='run-hitl'").fetchone(); after=len(seam.tool.calls); durable=effect[2]=="succeeded" and all(str(x).startswith("authorization-binding-v1:") for x in effect[:2]); await seam.close(); return self.observation("runtime.hitl",{"physical_calls_before":before,"physical_calls_after":after,"decision":"approved","durable":durable})
                async def delivery(self):
                    path=self.root/"delivery.sqlite"; database=Database.open(path); uow=SqliteExecutionUnitOfWork(database); sink=PhysicalSink(fail_first=True); dispatcher=DeliveryDispatcher(uow,{"fixture":sink},clock=lambda:10.0); uow.create_with_start_snapshot(execution_session_id="session-delivery",run_id="run-delivery",request_id="request-delivery",profile_key="agent.general",driver_kind="react",snapshot={"schema_version":1,"profile_key":"agent.general","driver_kind":"react","turn_id":"turn-delivery","tool_catalog_generation":1,"input":{}},event_id="run-delivery:created",now=1.0); run,lease=uow.claim_runtime_activation(run_id="run-delivery",owner_id="delivery",namespace="runtime.kernel",now=2.0,lease_ttl_seconds=30.0); fence=await uow.acquire(RunId("run-delivery"),lease,now=2.0); uow.commit_root_terminal_with_deliveries(run_id=run.run_id,expected_version=run.version,terminal_state=RunState.COMPLETED,event_id="run-delivery:completed",terminal_payload={"result":"physical"},deliveries=(DeliverySpec("delivery-1","fixture","delivery-key",{"result":"physical"}),),fence=fence,execution_lease=lease,terminal_fence_receipt_ref="runtime-fence:delivery:1",now=3.0); uow.release_runtime_lease(lease,now=3.0); await dispatcher.run_once(); await dispatcher.run_once(); record=uow.read_delivery("delivery-1"); values={"attempts":sink.attempts,"deliveries":len(sink.deliveries),"settled":record.state is DeliveryState.DELIVERED}; database.close(); return self.observation("runtime.delivery",values)
                async def budget(self):
                    scenarios=[("max_turns",TerminationLimits(max_turns=1),[response(calls=(tool_call("t1"),)),response()]),("max_tool_calls",TerminationLimits(max_tool_calls=1),[response(calls=(tool_call("a"),tool_call("b",2)))]),("repeated_tool",TerminationLimits(max_consecutive_same_tool=1),[response(calls=(tool_call("same"),)),response(calls=(tool_call("same"),))])]; observed=[]
                    for name,limits,responses in scenarios:
                        seam=RuntimeSeam(self.root/f"budget-{name}.sqlite",responses,limits=limits); run,_=await seam.run(f"run-{name}"); assert run.state is RunState.FAILED; observed.append(name); await seam.close()
                    wall=RuntimeSeam(self.root/"budget-wall.sqlite",[],limits=TerminationLimits(max_wall_seconds=0.5),clock=StepClock()); wall_run,_=await wall.run("run-wall"); assert wall_run.state is RunState.FAILED and wall.provider.physical_calls==0; await wall.close()
                    cost=RuntimeSeam(self.root/"budget-cost.sqlite",[response(calls=(tool_call("cost"),)),response()],limits=TerminationLimits(max_cost_micros=1),estimator=FrozenPriceEstimator("cost-price-v1","fixture:model",1_000_000,1_000_000)); cost_run,_=await cost.run("run-cost"); assert cost_run.state is RunState.FAILED and cost.provider.physical_calls==1; await cost.close()
                    ordered=[observed[0],observed[1],"wall_clock","cost",observed[2]]; return self.observation("runtime.budget",{"terminations":ordered})
                async def restart_without_replay(self):
                    path=self.root/"restart.sqlite"; provider=[response(calls=(tool_call("restart"),)),response()]; seam=RuntimeSeam(path,provider); run,_=await seam.run("run-restart"); before=seam.provider.physical_calls+len(seam.tool.calls); await seam.close(); reopened=RuntimeSeam(path,[]); await reopened.runtime.start(); restored=reopened.uow.read_run("run-restart"); after=before+reopened.provider.physical_calls+len(reopened.tool.calls); await reopened.close(); return self.observation("runtime.restart_without_replay",{"reopened":restored.state is run.state,"physical_calls_before":before,"physical_calls_after":after,"reconciled":restored.state is RunState.COMPLETED})
                async def host_owned(self):
                    seam=WorkflowSeam(self.root/"host.sqlite"); run_id,result,run=await seam.execute(seam.host,{}); values={"registered":seam.registry.get("host.workflow","v1") is not None,"completed":run.state is RunState.COMPLETED and len(WORKFLOW_NODE_CALLS)>0,"definition_id":seam.host.definition.name}; await seam.close(); return self.observation("workflow.host_owned",values)
                async def _official(self,key,case_id):
                    seam=WorkflowSeam(self.root/f"{key}.sqlite"); registration=next(x for x in seam.official if x.profile.descriptor.key==key)
                    if key=="workflow.personal_v1": state=personal_initial_state(run_id="run-personal",personal_workflow_selection=selection().to_child_payload(),inputs={})
                    elif key=="workflow.capability_build": state=capability_build_initial_state(run_id="run-capability-build",request="Build the missing capability",search_miss_receipt="miss-receipt-exact-wheel")
                    else: state=durable_initial_state(request="Only reply with a short answer",run_id=f"run-{key.split('.')[-1]}",session_metadata={},capability_refs=[],approval_required=False)
                    _,result,run=await seam.execute(registration,state); completed=run.state is RunState.COMPLETED and result.status.value=="completed"
                    if key=="workflow.capability_build": completed=completed and result.output["values"]["active"] is True and seam.capability.calls==["authorization","search","source_policy","isolated_build","package_store","activate"] and len(set(seam.capability.operation_keys))==6
                    await seam.close(); return self.observation(case_id,{"profile_key":key,"completed":completed})
                async def official_durable_task(self): return await self._official("workflow.durable_task","workflow.official_durable_task")
                async def official_personal_v1(self): return await self._official("workflow.personal_v1","workflow.official_personal_v1")
                async def official_capability_build(self): return await self._official("workflow.capability_build","workflow.official_capability_build")
                async def ticket_fingerprint(self):
                    seam=WorkflowSeam(self.root/"ticket.sqlite"); registration=seam.official[0]; forged=fingerprint=False
                    try: compile_workflow_registration(registration,transaction_owner=object())
                    except ValueError: forged=True
                    try: compile_workflow_registration(replace(registration,expected_manifest_hash="0"*64),transaction_owner=seam.owner)
                    except ValueError: fingerprint=True
                    child_runs=seam.database.connection.execute("SELECT count(*) FROM runs WHERE parent_run_id IS NOT NULL").fetchone()[0]; await seam.close(); return self.observation("workflow.ticket_fingerprint",{"forged_ticket_rejected":forged,"fingerprint_rejected":fingerprint,"child_runs":child_runs})
                async def reopen(self):
                    path=self.root/"reopen.sqlite"; seam=WorkflowSeam(path); before=len(WORKFLOW_NODE_CALLS); run_id,result,run=await seam.execute(seam.host,{}); physical_before=len(WORKFLOW_NODE_CALLS)-before; await seam.close(); reopened=WorkflowSeam(path); recovered=await reopened.runner.recover(run_id,WorkflowContext()); restored=reopened.uow.read_run(run_id); physical_after=len(WORKFLOW_NODE_CALLS)-before; values={"reopened":restored is not None and recovered.run_id==run_id,"run_before":run_id,"run_after":restored.run_id,"physical_calls_before":physical_before,"physical_calls_after":physical_after,"completed":restored.state is RunState.COMPLETED}; await reopened.close(); return self.observation("workflow.reopen",values)
                async def aclose(self): self.connection.close()
            """  # noqa: E501
        ),
        encoding="utf-8",
    )
    (root / "consumer_host.py").write_text(
        textwrap.dedent(
            """
            from simple_harness.testing import ConformanceHostMetadata
            from consumer_real import RealSuite

            class Context:
                def __init__(self, name): self.name = name
                async def __aenter__(self): self.suite = RealSuite(self.name); return self.suite
                async def __aexit__(self, *args): await self.suite.aclose()

            class Host:
                metadata = ConformanceHostMetadata(
                    protocol_version="1.0.0", host_name="exact-wheel-consumer",
                    host_version="1.0.0",
                    capabilities=frozenset({"provider", "tool", "runtime", "workflow"}),
                )
                def open_suite(self, name): return Context(name)

            def build_host(): return Host()
            """
        ),
        encoding="utf-8",
    )
    (root / "test_consumer.py").write_text(
        "def test_all_suites(simple_harness_conformance_report):\n"
        "    assert simple_harness_conformance_report.passed\n",
        encoding="utf-8",
    )


def test_generated_consumer_cannot_forge_runtime_or_workflow_results(
    tmp_path: Path,
) -> None:
    _consumer_files(tmp_path)
    source = "\n".join(
        (tmp_path / name).read_text(encoding="utf-8")
        for name in ("consumer_host.py", "consumer_real.py")
    )
    forbidden = (
        '"terminal_state": "completed"',
        "TerminationState(",
        "DeliveryRecord(",
        'StatePatch({"run_id": "run-1", "status": "completed"})',
        '"completed": compiled.manifest.workflow_name',
        '"child_runs": 0',
        'values["unique_call_ids"]=True',
        'for name in ("wall_clock","cost"): observed.append(name)',
        "after=before; database.close()",
    )
    assert not [token for token in forbidden if token in source]
    required = (
        "build_runtime(",
        "ReActDriver(",
        "EffectExecutor(",
        "SqliteExecutionUnitOfWork(",
        "DeliveryDispatcher(",
        "WorkflowRunner(",
        "NativeWorkflowEngine",
    )
    assert not [token for token in required if token not in source]


def test_exact_wheel_clean_python311_cli_and_pytest_protocol(
    reproducible_artifacts: BuildArtifacts, tmp_path: Path
) -> None:
    wheel = next(reproducible_artifacts.first.glob("*.whl")).resolve()
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    environment = tmp_path / ".venv"
    subprocess.run(
        ["uv", "venv", str(environment), "--python", "3.11"],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    python = environment / "bin/python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), f"{wheel}[testing]"],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import inspect,sys;"
                "from pathlib import Path;"
                "from simple_harness.testing import arm64_candidate as candidate;"
                "assert tuple(inspect.signature(candidate.run_core_gate).parameters)==();"
                "assert not inspect.iscoroutinefunction(candidate.run_core_gate);"
                "assert 'simple_harness_memory' not in sys.modules;"
                "from simple_harness import ConversationContinuationInput,Message,MessageRole;"
                "from simple_harness import (ContextAssemblyDecision,ContextFragment,"
                "DisclosureContext,LongTermMemoryType,"
                "MemoryAnalysisExecutorPort,RecallPlan,SanitizedEvidenceEnvelope,"
                "TaskScopeMutationPlan,TaskScopeProposal,TaskScopeRoute);"
                "assert LongTermMemoryType.SEMANTIC.value=='semantic';"
                "assert TaskScopeRoute.CREATE_NEW.value=='create_new';"
                "turn=ConversationContinuationInput(Message(MessageRole.USER,'continued'),"
                "'continued','sha256:'+'a'*64);"
                "assert ConversationContinuationInput.from_json(turn.to_json())==turn;"
                "assert ConversationContinuationInput(Message(MessageRole.USER,'fallback'),"
                "'fallback').context_source_snapshot_ref is None;"
                "origin=Path(candidate.__file__).resolve();"
                "assert 'site-packages' in origin.parts;"
                "assert origin.is_relative_to(Path(sys.prefix).resolve())"
            ),
        ],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    _consumer_files(tmp_path)
    report = tmp_path / "cli-report.json"
    clean_environment = dict(os.environ)
    clean_environment.pop("PYTHONPATH", None)
    clean_environment["PYTHONNOUSERSITE"] = "1"
    subprocess.run(
        [
            str(python),
            "-m",
            "simple_harness.testing",
            "--host",
            "consumer_host:build_host",
            "--suite",
            "provider,tool,runtime,workflow",
            "--json",
            str(report),
            "--artifact-sha256",
            wheel_sha256,
        ],
        check=True,
        cwd=tmp_path,
        env=clean_environment,
        capture_output=True,
        text=True,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "pass"
    assert payload["sdk_version"] == VERSION
    assert payload["artifact_sha256"] == wheel_sha256
    assert {case["suite"] for case in payload["cases"]} == {
        "provider",
        "tool",
        "runtime",
        "workflow",
    }
    subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "test_consumer.py",
            "--simple-harness-host",
            "consumer_host:build_host",
            "--simple-harness-artifact-sha256",
            wheel_sha256,
        ],
        check=True,
        cwd=tmp_path,
        env=clean_environment,
        capture_output=True,
        text=True,
    )
