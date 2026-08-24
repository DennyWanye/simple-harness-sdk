# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Product-neutral consumer using only the official Harness public composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from simple_harness import (
    AgentIdentity,
    AgentMemoryPort,
    ConsumerRuntimePolicies,
    ConsumerRuntimePorts,
    ConversationContextRequest,
    ConversationContextResult,
    ConversationContinuationInput,
    ConversationTurnInput,
    CurrentMessageContextProvider,
    FrozenJsonValue,
    JsonValue,
    Message,
    MessageRole,
    RunClient,
    RunId,
    Runtime,
    canonical_json,
    thaw_json,
)
from simple_harness.providers import ProviderRequest, ProviderResponse
from simple_harness.runtime import AuthorizationRequest, AuthorizationResult
from simple_harness.tools import (
    AuthorizationPort,
    CatalogRunToolExposure,
    ExecutableToolRecord,
    FunctionTool,
    JsonObject,
    RuntimeToolCatalog,
    Tool,
    ToolCall,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class DeterministicProvider:
    def __init__(self, *, blocked: bool = False) -> None:
        self.requests: list[ProviderRequest] = []
        self.blocked = blocked
        self.started = asyncio.Event()
        self.allow = asyncio.Event()

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:  # type: ignore[no-untyped-def]
        del cancel
        self.requests.append(request)
        self.started.set()
        if self.blocked:
            await self.allow.wait()
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "future consumer answer"),
            model="consumer-model",
            finish_reason="stop",
        )


class NoopTools:
    async def execute(self, call: ToolCall, context) -> ToolResult:  # type: ignore[no-untyped-def]
        raise AssertionError((call, context))


class AllowAuthorization:
    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        del request
        return AuthorizationResult("allow")


@dataclass(frozen=True, slots=True)
class FutureConsumerCapabilityFixture:
    """Neutral consumer composition: source metadata, permission, handler."""

    source: ExecutableToolRecord
    permission: AuthorizationPort
    handler: ToolHandler

    def build(
        self, *, generation: int = 1
    ) -> tuple[
        RuntimeToolCatalog,
        CatalogRunToolExposure,
        ToolRegistry,
    ]:
        catalog = RuntimeToolCatalog((self.source,), generation=generation)
        exposure = CatalogRunToolExposure(catalog)
        schema = thaw_json(cast(FrozenJsonValue, self.source.input_schema))
        if not isinstance(schema, dict):
            raise TypeError("executable tool schema must be an object")
        spec = ToolSpec(
            self.source.provider_name,
            self.source.description,
            cast(JsonObject, schema),
        )
        registry = ToolRegistry((cast(Tool, FunctionTool(spec, self.handler)),))
        return catalog, exposure, registry


class RichProductContextProvider:
    """Formal product Context with persona plus the current message, never Memory data."""

    def __init__(self) -> None:
        self.requests: list[ConversationContextRequest] = []

    async def prepare_once(self, request: ConversationContextRequest) -> ConversationContextResult:
        self.requests.append(request)
        persona = Message(
            MessageRole.SYSTEM,
            "Answer concisely for this product.",
            metadata={"source": "product_persona"},
        )
        payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "source_snapshot_ref": request.source_snapshot_ref,
            "provider_messages": [persona.to_dict(), request.current_message.to_dict()],
            "current_message": request.current_message.to_dict(),
        }
        return ConversationContextResult(
            request.preparation_id,
            request.source_snapshot_ref,
            payload,
            2,
            len(canonical_json(payload).encode("utf-8")),
        )


@dataclass(slots=True)
class FutureConsumerFixture:
    database_path: Path
    memory: AgentMemoryPort | None
    rich_context: bool = False
    block_provider: bool = False
    provider: DeterministicProvider = field(init=False)
    context_provider: CurrentMessageContextProvider | RichProductContextProvider = field(init=False)

    def __post_init__(self) -> None:
        self.provider = DeterministicProvider(blocked=self.block_provider)
        self.context_provider = (
            RichProductContextProvider() if self.rich_context else CurrentMessageContextProvider()
        )

    async def build(self) -> Runtime:
        from simple_harness import build_consumer_runtime

        return await build_consumer_runtime(
            ConsumerRuntimePorts(
                provider=self.provider,
                tool_executor=NoopTools(),
                authorization=AllowAuthorization(),
                database_path=str(self.database_path),
                memory=self.memory,
                context_provider=self.context_provider,
                policies=ConsumerRuntimePolicies.local_default(),
            )
        )

    @staticmethod
    def identity(*, household: str, actor: str, session: str) -> AgentIdentity:
        return AgentIdentity("future-consumer", household, actor, session)

    @staticmethod
    async def complete_turn(
        runtime: Runtime,
        *,
        identity: AgentIdentity,
        run_id: str,
        text: str,
        context_source_snapshot_ref: str | None = None,
    ) -> None:
        typed_run_id = RunId(run_id)
        await RunClient(runtime).start_conversation(
            ConversationTurnInput(
                identity,
                Message(MessageRole.USER, text),
                text,
                context_source_snapshot_ref=context_source_snapshot_ref,
            ),
            run_id=typed_run_id,
        )
        await runtime.wait_idle(typed_run_id)

    @staticmethod
    async def start_turn(
        runtime: Runtime,
        *,
        identity: AgentIdentity,
        run_id: str,
        text: str,
        context_source_snapshot_ref: str | None = None,
    ) -> RunId:
        typed_run_id = RunId(run_id)
        await RunClient(runtime).start_conversation(
            ConversationTurnInput(
                identity,
                Message(MessageRole.USER, text),
                text,
                context_source_snapshot_ref=context_source_snapshot_ref,
            ),
            run_id=typed_run_id,
        )
        return typed_run_id

    @staticmethod
    async def continue_turn(
        runtime: Runtime,
        *,
        run_id: RunId,
        continuation_id: str,
        text: str,
        context_source_snapshot_ref: str | None = None,
    ) -> None:
        await RunClient(runtime).signal_conversation(
            run_id,
            continuation_id=continuation_id,
            value=ConversationContinuationInput(
                Message(MessageRole.USER, text),
                text,
                context_source_snapshot_ref,
            ),
        )


__all__ = (
    "AllowAuthorization",
    "DeterministicProvider",
    "FutureConsumerFixture",
    "FutureConsumerCapabilityFixture",
    "NoopTools",
    "RichProductContextProvider",
)
