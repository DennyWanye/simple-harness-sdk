# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``agent.delegate``: single-level, quota-bound, non-recursive delegation to a child BaseAgent.

Design (Slice 1 plan v2.1, closure E1–E5, E8, E9):

* the child is a full BaseAgent that never reaches a terminal state, so the
  result channel is the child's ``base_agent_turn_results_v1`` row, never
  ``child_terminal_receipts``;
* one delegation = one child AgentTurn (the objective is its only input);
* recursion is prevented structurally: the child's capability snapshot never
  lists ``agent.delegate``;
* the delegation row is written before the child Run exists (with its
  ``base-agent/v1`` fence) and is *resumed*, never skipped, on retry;
* a timeout is a visible FAILED tool result, not an exception, and never a second
  launch.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from simple_harness.contracts import (
    ExecutionSessionId,
    FrozenJsonValue,
    JsonValue,
    Message,
    MessageRole,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ProfileLaunchTicket,
    child_launch_fingerprint,
)
from simple_harness.execution.effects import EffectRecord
from simple_harness.execution.sqlite.base_agent.delegations import DelegationQuotaExceeded
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime.child_runs import ChildLaunchRequest, ProfileLaunchTicketRef
from simple_harness.runtime.start_snapshot import RunStart, bind_start_snapshot
from simple_harness.tools import FunctionTool, ToolContext, ToolResult, ToolSpec
from simple_harness.tools.contracts import ToolHandler, ToolOutcome
from simple_harness.tools.reconciliation import ReconciliationObservation, ReconciliationState
from simple_harness.tools.runtime_catalog import (
    ToolEffectClass,
    ToolExecutionPolicy,
    ToolRouteRequirement,
    ToolTaskScopeRequirement,
)

from ..config import AgentConfig, config_hash
from ..contracts import AgentTurnResult

if TYPE_CHECKING:
    from ..runtime import AgentRuntime

# OpenAI-compatible endpoints only accept ``^[A-Za-z0-9_-]+$`` function names, so the
# durable identifier is ``agent_delegate``; documents call the capability "agent.delegate".
DELEGATE_TOOL_NAME = "agent_delegate"
CHILD_PROFILE_KEY = "agent.base"
BASE_AGENT_DRIVER_KIND = "base_agent"
CHILD_INPUT_ID = "objective"
MAX_OBJECTIVE_LENGTH = 8000
MAX_DELEGATION_ID_LENGTH = 128
INLINE_RESULT_LIMIT_BYTES = 16 * 1024

DELEGATE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "objective": {"type": "string", "maxLength": MAX_OBJECTIVE_LENGTH},
        "role_hint": {"type": "string", "enum": ["worker"]},
        "delegation_id": {"type": "string", "maxLength": MAX_DELEGATION_ID_LENGTH},
    },
    "required": ["objective", "delegation_id"],
}

DELEGATE_EXECUTION_POLICY = ToolExecutionPolicy(
    capability_id=DELEGATE_TOOL_NAME,
    capability_fingerprint=hashlib.sha256(
        canonical_json({"tool": DELEGATE_TOOL_NAME, "schema": DELEGATE_INPUT_SCHEMA}).encode()
    ).hexdigest(),
    effect_class=ToolEffectClass.NON_PROJECT_EFFECT,
    route_requirement=ToolRouteRequirement.FORBIDDEN,
    task_scope_requirement=ToolTaskScopeRequirement.FORBIDDEN,
)


def child_agent_id_for(parent_agent_id: str, delegation_id: str) -> str:
    digest = hashlib.sha256(
        f"{parent_agent_id}\x00delegation\x00{delegation_id}".encode()
    ).hexdigest()
    return f"agent-{digest[:32]}"


def _rejected(context: ToolContext, code: str, message: str) -> ToolResult:
    return ToolResult(
        call_id=context.call_id,  # type: ignore[arg-type]
        outcome=ToolOutcome.REJECTED,
        error_code=code,
        public_message=message,
    )


def _failed(context: ToolContext, code: str, message: str, value: JsonValue = None) -> ToolResult:
    return ToolResult(
        call_id=context.call_id,  # type: ignore[arg-type]
        outcome=ToolOutcome.FAILED,
        value=cast(FrozenJsonValue, value),
        error_code=code,
        public_message=message,
        retryable=False,
    )


def _result_payload(
    result_json: Mapping[str, JsonValue], result_hash: str, turn_id: str
) -> JsonValue:
    body = thaw_json(result_json)  # type: ignore[arg-type]
    encoded = canonical_json(body)
    if len(encoded.encode("utf-8")) <= INLINE_RESULT_LIMIT_BYTES:
        return body
    decoded = AgentTurnResult.from_json(body)
    text = ""
    if decoded.public_output is not None and isinstance(decoded.public_output.content, str):
        text = decoded.public_output.content
    # Truncate the *reference* handed to the model, never the durable fact.
    return {
        "summary": text[:2000],
        "truncated": True,
        "result_ref": f"base_agent_turn_result:{turn_id}",
        "result_hash": result_hash,
    }


class AgentDelegateTool:
    """Late-bound to the ``AgentRuntime`` after the kernel exists (registry is sealed first)."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._runtime: AgentRuntime | None = None
        self._clock = clock
        self.launches = 0  # observable for tests: physical child launches performed here
        self.fault: Callable[[str], None] | None = None  # crash-injection hook (tests only)

    def _fault(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)

    def bind(self, runtime: AgentRuntime) -> None:
        if self._runtime is not None and self._runtime is not runtime:
            raise RuntimeError("agent.delegate is already bound to another runtime")
        self._runtime = runtime

    def function_tool(self) -> FunctionTool:
        spec = ToolSpec(
            DELEGATE_TOOL_NAME,
            "把一个明确的子目标委派给一个新的工作 Agent，等待它的结论并原样返回。"
            "每轮委派次数有上限；delegation_id 相同则返回同一个子 Agent 的结果。",
            DELEGATE_INPUT_SCHEMA,
        )
        return FunctionTool(spec, cast(ToolHandler, self.handle))

    @property
    def runtime(self) -> AgentRuntime:
        if self._runtime is None:
            raise RuntimeError("agent.delegate is not bound to an AgentRuntime")
        return self._runtime

    async def handle(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        runtime = self.runtime
        uow = runtime.uow
        objective = arguments.get("objective")
        delegation_id = arguments.get("delegation_id")
        if not isinstance(objective, str) or not isinstance(delegation_id, str):
            return _rejected(
                context,
                "agent_delegation_invalid_arguments",
                "objective and delegation_id are required",
            )
        if len(objective) > MAX_OBJECTIVE_LENGTH:
            return _rejected(
                context,
                "agent_delegation_objective_too_large",
                f"objective exceeds {MAX_OBJECTIVE_LENGTH} characters; split the work",
            )
        if not objective.strip() or not delegation_id.strip():
            return _rejected(
                context,
                "agent_delegation_invalid_arguments",
                "objective and delegation_id must not be blank",
            )

        parent_run_id = context.run_id.value
        parent = uow.read_agent_binding_for_run(parent_run_id)
        if parent is None:
            return _rejected(
                context, "agent_delegation_parent_unbound", "the calling Run is not a BaseAgent"
            )
        if parent.role != "root":
            # Structural single level (closure review F1): a delegated worker never delegates.
            return _rejected(
                context, "agent_delegation_not_permitted", "a delegated Agent cannot delegate"
            )
        parent_turn = uow.read_open_agent_turn(parent_run_id)
        if parent_turn is None:
            return _rejected(
                context, "agent_delegation_no_open_turn", "no open AgentTurn on the parent"
            )
        parent_config = AgentConfig.from_json(thaw_json(parent.config_json))
        limits = parent_config.limits

        existing = uow.read_agent_delegation(delegation_id)
        if existing is not None and (
            existing.parent_agent_id != parent.agent_id
            or existing.parent_turn_id != parent_turn.turn_id
        ):
            return _rejected(
                context,
                "agent_delegation_unknown_id",
                "delegation_id belongs to another Agent or turn; choose a fresh delegation_id",
            )
        if (
            existing is None
            and uow.count_agent_delegations(parent_turn.turn_id) >= limits.max_delegations_per_turn
        ):
            return _rejected(
                context,
                "agent_delegation_quota_exceeded",
                f"at most {limits.max_delegations_per_turn} delegation(s) per turn; "
                "reuse the existing result",
            )

        child_agent_id = child_agent_id_for(parent.agent_id, delegation_id)
        child_run_id = child_agent_id
        ticket_id = f"{child_agent_id}:ticket"
        command_id = f"{child_agent_id}:launch"
        request_id = f"{child_agent_id}:create"
        child_config = AgentConfig(
            name=f"{parent_config.name}/worker",
            instructions=runtime.ports.child_instructions_template,
            model_profile_ref=parent_config.model_profile_ref,
            tool_names=tuple(
                name for name in parent_config.tool_names if name != DELEGATE_TOOL_NAME
            ),
            limits=limits,
        )
        from ..runtime import start_input_for

        child_input = start_input_for(
            child_config,
            agent_id=child_agent_id,
            role="child",
            owner_scope=parent.owner_scope,
            max_output_tokens=runtime.ports.default_max_output_tokens,
        )
        intent_hash = hashlib.sha256(
            canonical_json(
                {"delegation_id": delegation_id, "child_run_id": child_run_id, "input": child_input}
            ).encode()
        ).hexdigest()
        launch_payload: dict[str, JsonValue] = {
            "profile_key": CHILD_PROFILE_KEY,
            "driver_kind": BASE_AGENT_DRIVER_KIND,
            "catalog_generation": 1,
            "delegation_id": delegation_id,
            "child_agent_id": child_agent_id,
        }
        now = self._clock()

        # 1. Pre-fence + reserved delegation row (idempotent by delegation_id).
        try:
            delegation = uow.reserve_child_base_agent_delegation(
                delegation_id=delegation_id,
                parent_agent_id=parent.agent_id,
                parent_turn_id=parent_turn.turn_id,
                child_agent_id=child_agent_id,
                child_run_id=child_run_id,
                ticket_id=ticket_id,
                intent_hash=intent_hash,
                now=now,
                max_per_turn=limits.max_delegations_per_turn,
            )
        except DelegationQuotaExceeded:
            return _rejected(
                context,
                "agent_delegation_quota_exceeded",
                f"at most {limits.max_delegations_per_turn} delegation(s) per turn; "
                "reuse the existing result",
            )
        self._fault("delegate.after_reserve")
        # 2. Resume from wherever the previous attempt stopped (closure E4).
        if delegation.state == "settled":
            return self._settled_result(context, child_agent_id, delegation_id)
        if delegation.state == "failed":
            return _failed(
                context,
                "agent_delegation_failed",
                "the delegation already failed",
                {
                    "child_agent_id": child_agent_id,
                    "delegation_id": delegation_id,
                    "status": "failed",
                },
            )
        if uow.read_run(child_run_id) is None:
            uow.issue_profile_launch_ticket(
                ProfileLaunchTicket(
                    ticket_id,
                    parent_run_id,
                    CHILD_PROFILE_KEY,
                    1,
                    child_launch_fingerprint(launch_payload),
                ),
                now=now,
            )
            driver = runtime.driver
            snapshot = bind_start_snapshot(
                RunStart(
                    ExecutionSessionId(f"base-agent:{child_agent_id}"),
                    RunId(child_run_id),
                    RequestId(request_id),
                    f"{child_agent_id}:start",
                    child_input,
                    1,
                ),
                profile_key=CHILD_PROFILE_KEY,
                driver_kind=BASE_AGENT_DRIVER_KIND,
                policy_fingerprint=getattr(driver, "policy_fingerprint", None),
            ).to_json()
            self.launches += 1
            await runtime.kernel.children.launch(
                ChildLaunchRequest(
                    ProfileLaunchTicketRef(ticket_id, 1),
                    command_id,
                    child_run_id,
                    request_id,
                    AttachmentPolicy.DETACHED,
                    cast(FrozenJsonValue, launch_payload),
                    cast(FrozenJsonValue, snapshot),
                )
            )
        self._fault("delegate.after_launch")
        if uow.read_agent_binding(child_agent_id) is None:
            uow.create_agent_binding(
                agent_id=child_agent_id,
                run_id=child_run_id,
                owner_scope=parent.owner_scope,
                role="child",
                creation_key=f"delegation:{parent.agent_id}:{delegation_id}",
                config_json=child_config.to_json(),
                config_hash=config_hash(child_config),
                now=now,
            )
        if delegation.state == "reserved":
            uow.set_agent_delegation_state(delegation_id=delegation_id, state="launched", now=now)
        # 3. The child's single input (idempotent by input_id).
        child_turn_id = f"{child_agent_id}:input:{CHILD_INPUT_ID}"
        message = Message(MessageRole.USER, objective).to_dict()
        input_hash = hashlib.sha256(canonical_json({"message": message}).encode()).hexdigest()
        try:
            await runtime.kernel.signal_base_agent_input(
                RunId(child_run_id),
                agent_id=child_agent_id,
                turn_id=child_turn_id,
                input_id=CHILD_INPUT_ID,
                input_hash=input_hash,
                input_json={"message": message},
                message=message,
            )
        except UnitOfWorkConflict as error:
            return _failed(
                context,
                "agent_delegation_input_conflict",
                str(error),
                {
                    "child_agent_id": child_agent_id,
                    "delegation_id": delegation_id,
                    "status": "launched",
                },
            )
        # 4. Condition-wait on the child's result row (never wait_idle, never in a transaction).
        deadline = self._clock() + limits.delegation_wait_seconds
        interval = 0.01
        while True:
            stored = uow.read_agent_turn_result(child_turn_id)
            if stored is not None:
                break
            if self._clock() >= deadline:
                return _failed(
                    context,
                    "agent_delegation_timeout",
                    "the delegated Agent has not finished yet; its result stays readable later",
                    {
                        "child_agent_id": child_agent_id,
                        "delegation_id": delegation_id,
                        "status": "pending",
                    },
                )
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.2)
        uow.set_agent_delegation_state(
            delegation_id=delegation_id, state="settled", now=self._clock()
        )
        return self._settled_result(context, child_agent_id, delegation_id)

    def _settled_result(
        self, context: ToolContext, child_agent_id: str, delegation_id: str
    ) -> ToolResult:
        uow = self.runtime.uow
        child_turn_id = f"{child_agent_id}:input:{CHILD_INPUT_ID}"
        stored = uow.read_agent_turn_result(child_turn_id)
        if stored is None:
            return _failed(
                context,
                "agent_delegation_result_missing",
                "settled delegation has no result row",
                {
                    "child_agent_id": child_agent_id,
                    "delegation_id": delegation_id,
                    "status": "settled",
                },
            )
        return ToolResult.succeeded(
            context.call_id,  # type: ignore[arg-type]
            {
                "child_agent_id": child_agent_id,
                "delegation_id": delegation_id,
                "status": "settled",
                "result_hash": stored.result_hash,
                "result": _result_payload(stored.result_json, stored.result_hash, child_turn_id),  # type: ignore[arg-type]
            },
        )


class AgentDelegationReconciliation:
    """UNKNOWN ``agent.delegate`` effects settle from the child's result row (never relaunch)."""

    def __init__(self, uow, fallback) -> None:  # type: ignore[no-untyped-def]
        self._uow = uow
        self._fallback = fallback

    async def observe(self, effect: EffectRecord) -> ReconciliationObservation:
        if effect.tool_name != DELEGATE_TOOL_NAME:
            return await self._fallback.observe(effect)
        arguments = thaw_json(effect.arguments)
        delegation_id = arguments.get("delegation_id") if isinstance(arguments, dict) else None
        pending_ref = f"base_agent_delegation:{delegation_id}:pending"
        if not isinstance(delegation_id, str):
            return ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, pending_ref)
        delegation = self._uow.read_agent_delegation(delegation_id)
        if delegation is None:
            return ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, pending_ref)
        child_turn_id = f"{delegation.child_agent_id}:input:{CHILD_INPUT_ID}"
        stored = self._uow.read_agent_turn_result(child_turn_id)
        if stored is None:
            return ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, pending_ref)
        if delegation.state in {"reserved", "launched"}:
            self._uow.set_agent_delegation_state(
                delegation_id=delegation_id, state="launched", now=time.time()
            )
            self._uow.set_agent_delegation_state(
                delegation_id=delegation_id, state="settled", now=time.time()
            )
        result = ToolResult.succeeded(
            effect.call_id,
            {
                "child_agent_id": delegation.child_agent_id,
                "delegation_id": delegation_id,
                "status": "settled",
                "result_hash": stored.result_hash,
                "result": _result_payload(stored.result_json, stored.result_hash, child_turn_id),  # type: ignore[arg-type]
            },
        )
        return ReconciliationObservation(
            ReconciliationState.COMPLETED, stored.commit_receipt_id, result
        )


__all__ = (
    "DELEGATE_EXECUTION_POLICY",
    "DELEGATE_INPUT_SCHEMA",
    "DELEGATE_TOOL_NAME",
    "AgentDelegateTool",
    "AgentDelegationReconciliation",
    "child_agent_id_for",
)
