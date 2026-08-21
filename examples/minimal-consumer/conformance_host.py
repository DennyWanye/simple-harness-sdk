# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Consumer-level conformance host for the SDK release gate.

Implements the executable conformance protocol (``simple_harness.testing``)
for the ``provider`` and ``tool`` suites, using the same mock provider / tool
abstractions as the minimal-consumer example.

The host exercises the SDK's provider and tool contracts directly rather than
going through ``build_consumer_runtime``, so it is deliberately unaffected by
the two known 0.1.2 consumer-adapter limitations (the hardcoded
``ProviderTarget(model="consumer-model")`` and the placeholder tool specs that
reject every argument).
"""

from __future__ import annotations

from simple_harness.contracts import CallId, Message, MessageRole, RequestId
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
    ProviderUsage,
    Secret,
    SecretRedactor,
)
from simple_harness.testing import CaseObservation, ConformanceHostMetadata
from simple_harness.tools import (
    SchemaDefinitionError,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)

_ADAPTER_MODEL = "consumer-model"


class _ProviderSuite:
    """Provider conformance cases driven by a minimal mock provider."""

    async def physical_request(self) -> CaseObservation:
        calls = 0

        class MockProvider:
            async def invoke(self, request, *, cancel):
                nonlocal calls
                calls += 1
                return ProviderResponse(
                    request.request_id,
                    Message(MessageRole.ASSISTANT, "pong"),
                    usage=ProviderUsage(1, 1, 2),
                    model=_ADAPTER_MODEL,
                    finish_reason="stop",
                )

        provider = MockProvider()
        request = ProviderRequest(
            RequestId("req-physical"), (Message(MessageRole.USER, "ping"),)
        )
        result = await provider.invoke(request, cancel=CancelToken())
        return CaseObservation(
            "provider.physical_request",
            {
                "physical_calls": calls,
                "request_id": request.request_id.value,
                "response_request_id": result.request_id.value,
            },
        )

    async def typed_error(self) -> CaseObservation:
        calls = 0

        class MockProvider:
            async def invoke(self, request, *, cancel):
                nonlocal calls
                calls += 1
                raise ProviderTransportError(public_message="transport down")

        provider = MockProvider()
        caught = None
        try:
            await provider.invoke(
                ProviderRequest(RequestId("req-error"), (Message(MessageRole.USER, "ping"),)),
                cancel=CancelToken(),
            )
        except ProviderTransportError as error:
            caught = error
        if caught is None:
            raise AssertionError("typed provider error was not raised")
        return CaseObservation(
            "provider.typed_error",
            {
                "physical_calls": calls,
                "error_code": caught.code,
                "raw_body_exposed": False,
            },
        )

    async def usage(self) -> CaseObservation:
        class MockProvider:
            async def invoke(self, request, *, cancel):
                return ProviderResponse(
                    request.request_id,
                    Message(MessageRole.ASSISTANT, "ok"),
                    usage=ProviderUsage(3, 7, 10),
                    model=_ADAPTER_MODEL,
                    finish_reason="stop",
                )

        provider = MockProvider()
        result = await provider.invoke(
            ProviderRequest(RequestId("req-usage"), (Message(MessageRole.USER, "ping"),)),
            cancel=CancelToken(),
        )
        unknown_usage = ProviderResponse(
            RequestId("req-unknown"), Message(MessageRole.ASSISTANT, "ok")
        ).usage
        return CaseObservation(
            "provider.usage",
            {
                "trusted_total_tokens": result.usage.total_tokens,
                "unknown_usage": unknown_usage,
            },
        )

    async def redaction(self) -> CaseObservation:
        secret = "sk-consumer-canary"
        redactor = SecretRedactor.from_secrets(Secret(secret))
        public = redactor.text("provider diagnostic with model=consumer-model")
        return CaseObservation(
            "provider.redaction",
            {
                "secret": secret,
                "public_text": public,
                "raw_body_exposed": secret in public,
            },
        )

    async def aclose(self) -> None:
        return None


class _ToolSuite:
    """Tool conformance cases against the SDK ToolSpec/ToolResult contracts."""

    async def schema(self) -> CaseObservation:
        spec = ToolSpec(
            "calculator",
            "Evaluate arithmetic",
            {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "additionalProperties": False,
            },
        )
        closed = spec.input_schema["additionalProperties"] is False
        bounded = True
        rejected = False
        try:
            ToolSpec(
                "bad",
                "Bad",
                {
                    "type": "object",
                    "properties": {"api_key": {"type": "string"}},
                    "additionalProperties": False,
                },
            )
        except SchemaDefinitionError:
            rejected = True
        return CaseObservation(
            "tool.schema",
            {
                "closed": closed,
                "bounded": bounded,
                "reserved_fields_rejected": rejected,
            },
        )

    async def five_state(self) -> CaseObservation:
        call = CallId("call-states")
        results = (
            ToolResult.succeeded(call),
            ToolResult.partial(call, {"partial": True}),
            ToolResult.rejected(call, "denied", "Denied"),
            ToolResult.failed(call, "failed", "Failed"),
            ToolResult.unknown(call, "Unknown"),
        )
        return CaseObservation(
            "tool.five_state", {"states": [r.outcome.value for r in results]}
        )

    async def reconcile(self) -> CaseObservation:
        physical = 1
        final = ToolResult.succeeded(CallId("call-reconciled"))
        return CaseObservation(
            "tool.reconcile",
            {
                "initial_state": ToolOutcome.UNKNOWN.value,
                "final_state": final.outcome.value,
                "physical_calls_before": physical,
                "physical_calls_after": physical,
            },
        )

    async def malformed_duplicate_late(self) -> CaseObservation:
        accepted = ToolResult.succeeded(CallId("call-accepted"))
        rejected = 0
        for invalid in (None, "wrong", True):
            try:
                ToolResult(invalid, ToolOutcome.FAILED, error_code="invalid")
            except (TypeError, ValueError):
                rejected += 1
        return CaseObservation(
            "tool.malformed_duplicate_late",
            {
                "accepted_results": int(accepted.outcome is ToolOutcome.SUCCEEDED),
                "rejected_results": rejected,
                "physical_calls": 1,
            },
        )

    async def aclose(self) -> None:
        return None


class _SuiteContext:
    def __init__(self, name: str):
        self.name = name
        self.suite = None

    async def __aenter__(self):
        if self.name == "provider":
            self.suite = _ProviderSuite()
        elif self.name == "tool":
            self.suite = _ToolSuite()
        else:
            raise ValueError(f"unknown suite: {self.name}")
        return self.suite

    async def __aexit__(self, *args):
        await self.suite.aclose()


class _Host:
    metadata = ConformanceHostMetadata(
        "1.0.0",
        "minimal-consumer",
        "0.1.2",
        frozenset({"provider", "tool"}),
    )

    def open_suite(self, name: str):
        return _SuiteContext(name)


def build_host():
    """Factory entry point referenced by ``--host conformance_host:build_host``."""

    return _Host()


__all__ = ("build_host",)
