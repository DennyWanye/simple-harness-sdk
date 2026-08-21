# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError

import pytest

from simple_harness.contracts import CallId, EffectId, RequestId, RunId
from simple_harness.tools import (
    CancellationToken,
    DuplicateToolCallError,
    EffectKind,
    EffectPolicy,
    FunctionTool,
    LateToolResultError,
    MalformedToolArgumentsError,
    SchemaDefinitionError,
    Sidecar,
    ToolCall,
    ToolCallState,
    ToolContext,
    ToolDispatchKind,
    ToolInventoryRecord,
    ToolOutcome,
    ToolOutcomeParser,
    ToolRegistry,
    ToolResource,
    ToolResult,
    ToolSpec,
    builtin_outcome_parser,
    parse_tool_outcome,
)


def _spec() -> ToolSpec:
    return ToolSpec(
        name="project_summary",
        description="Read a bounded project summary.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 40,
                },
                "format": {
                    "type": "string",
                    "enum": ["short", "long"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 8},
                    "maxItems": 2,
                },
            },
            "required": ["path", "format"],
            "additionalProperties": False,
        },
    )


def _context(token: CancellationToken | None = None) -> ToolContext:
    return ToolContext(
        run_id=RunId("run-1"),
        request_id=RequestId("request-1"),
        cancellation=token or CancellationToken(),
    )


def test_typed_sidecar_is_frozen_bound_to_spec_and_registry_digest() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    from simple_harness.contracts import canonical_json

    parser = builtin_outcome_parser(ToolOutcomeParser.JSON_ERROR_ENVELOPE)
    sidecar = Sidecar(
        ToolInventoryRecord(
            name="project_summary",
            access="read",
            spec_version="v1",
            schema_hash=hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
            handler_id="builtin.project-summary.v1",
            dispatch_kind=ToolDispatchKind.SYNC,
            effect_policy=EffectPolicy(
                "builtin:project-summary:read",
                "v1",
                EffectKind.IDEMPOTENT_READ,
            ),
            outcome_parser_id=ToolOutcomeParser.JSON_ERROR_ENVELOPE,
            outcome_parser_version="v1",
            outcome_parser_hash=parser.parser_hash,
            execution_build_digest="a" * 64,
        ),
        outcome_parser=parser,
    )
    spec = ToolSpec(
        "project_summary",
        "Read summary.",
        schema,
        sidecar=sidecar,
    )
    registry = ToolRegistry(
        [FunctionTool(spec, lambda _args, context: ToolResult.succeeded(context.call_id))]
    )
    assert spec.sidecar is sidecar
    assert registry.sidecars["project_summary"].digest == sidecar.digest
    assert len(registry.inventory_digest) == 64
    with pytest.raises((AttributeError, TypeError)):
        sidecar.inventory.handler_id = "tampered"  # type: ignore[misc]
    with pytest.raises(ValueError, match="name"):
        ToolSpec(
            "foreign",
            "Foreign.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            sidecar=sidecar,
        )
    assert registry.seal(require_sidecars=True) == registry.inventory_digest
    with pytest.raises(RuntimeError, match="sealed"):
        registry.register(FunctionTool(spec, lambda *_: ToolResult.succeeded(CallId("x"))))


def test_tool_context_rejects_mismatched_call_and_effect_identity() -> None:
    context = ToolContext(
        RunId("run-1"),
        RequestId("request-1"),
        CancellationToken(),
        {},
        None,
        CallId("call-1"),
        EffectId("effect-1"),
    )
    assert context.call_id == CallId("call-1")
    assert context.effect_id == EffectId("effect-1")
    with pytest.raises(TypeError, match="call_id"):
        ToolContext(
            RunId("run-1"),
            RequestId("request-1"),
            CancellationToken(),
            call_id="call-1",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("parser", "raw", "outcome", "error_code"),
    [
        (ToolOutcomeParser.SHELL_EXIT, {"exit_code": 9}, ToolOutcome.FAILED, "shell_nonzero_exit"),
        (
            ToolOutcomeParser.SHELL_EXIT,
            {"stdout": "ok"},
            ToolOutcome.FAILED,
            "malformed_shell_outcome",
        ),
        (
            ToolOutcomeParser.ARTIFACT_ENVELOPE,
            {"ok": True},
            ToolOutcome.FAILED,
            "artifact_path_missing",
        ),
        (
            ToolOutcomeParser.CODE_ARRAY_OR_ERROR,
            {"code": "print(1)"},
            ToolOutcome.FAILED,
            "malformed_code_outcome",
        ),
        (
            ToolOutcomeParser.ACTIVATION_PROPOSED,
            {"state": "proposed"},
            ToolOutcome.FAILED,
            "malformed_activation",
        ),
        (
            ToolOutcomeParser.JSON_ERROR_ENVELOPE,
            {"status": "pending"},
            ToolOutcome.UNKNOWN,
            "tool_outcome_unknown",
        ),
        (
            ToolOutcomeParser.JSON_ERROR_ENVELOPE,
            {"status": "unknown"},
            ToolOutcome.UNKNOWN,
            "tool_outcome_unknown",
        ),
    ],
)
def test_sdk_owned_outcome_parsers_fail_closed(
    parser: ToolOutcomeParser,
    raw: object,
    outcome: ToolOutcome,
    error_code: str,
) -> None:
    result = parse_tool_outcome(CallId("call-parser"), raw, parser)
    assert result.outcome is outcome
    assert result.error_code == error_code


def test_registry_invokes_typed_parser_and_overwrites_untrusted_call_identity() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    from simple_harness.contracts import canonical_json

    parser = builtin_outcome_parser(ToolOutcomeParser.SHELL_EXIT)
    contexts: list[ToolContext] = []

    def raw_handler(_arguments: object, context: ToolContext) -> object:
        contexts.append(context)
        return {"exit_code": 0, "stdout": "ok"}

    sidecar = Sidecar(
        ToolInventoryRecord(
            name="shell",
            access="write",
            spec_version="v1",
            schema_hash=hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
            outcome_parser_id=parser.parser_id,
            outcome_parser_version=parser.version,
            outcome_parser_hash=parser.parser_hash,
        ),
        outcome_parser=parser,
    )
    registry = ToolRegistry([FunctionTool(ToolSpec("shell", "Run.", schema, sidecar), raw_handler)])
    untrusted = ToolContext(
        RunId("run-1"),
        RequestId("request-same"),
        CancellationToken(),
        call_id=CallId("forged"),
    )

    async def invoke_both():
        return (
            await registry.invoke(ToolCall(CallId("call-a"), "shell", {}), untrusted),
            await registry.invoke(ToolCall(CallId("call-b"), "shell", {}), untrusted),
        )

    first, second = asyncio.run(invoke_both())
    assert first.outcome is ToolOutcome.SUCCEEDED
    assert second.outcome is ToolOutcome.SUCCEEDED
    assert [value.call_id for value in contexts] == [CallId("call-a"), CallId("call-b")]


class _Resolver:
    resolver_id = "sdk.test.filesystem"
    version = "v1"

    def resolve(self, arguments, context):
        assert context.call_id == CallId("call-resource")
        return (ToolResource("filesystem", str(arguments["path"]), ("write",)),)


def test_typed_resource_resolver_is_consumed_by_sidecar() -> None:
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    from simple_harness.contracts import canonical_json

    sidecar = Sidecar(
        ToolInventoryRecord(
            name="write",
            access="write",
            spec_version="v1",
            schema_hash=hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
            resource_scope_resolver_id=_Resolver.resolver_id,
            resource_scope_resolver_version=_Resolver.version,
        ),
        resource_scope_resolver=_Resolver(),
    )
    resources = asyncio.run(
        sidecar.resolve_resources(
            {"path": "/tmp/out"},
            ToolContext(
                RunId("run-1"),
                RequestId("request-1"),
                CancellationToken(),
                call_id=CallId("call-resource"),
            ),
        )
    )
    assert resources == (ToolResource("filesystem", "/tmp/out", ("write",)),)


def test_registry_seal_rejects_missing_sidecar() -> None:
    registry = ToolRegistry(
        [FunctionTool(_spec(), lambda *_: ToolResult.succeeded(CallId("call-1")))]
    )
    with pytest.raises(ValueError, match="sidecars are required"):
        registry.seal(require_sidecars=True)
    assert not registry.sealed


def test_sidecar_rejects_metadata_without_matching_typed_ports() -> None:
    schema_hash = "a" * 64
    with pytest.raises(ValueError, match="typed parser port"):
        Sidecar(
            ToolInventoryRecord(
                name="parse",
                access="read",
                spec_version="v1",
                schema_hash=schema_hash,
                outcome_parser_id=ToolOutcomeParser.JSON_ERROR_ENVELOPE,
                outcome_parser_version="v1",
                outcome_parser_hash="b" * 64,
            )
        )
    with pytest.raises(ValueError, match="resolver port differs"):
        Sidecar(
            ToolInventoryRecord(
                name="resolve",
                access="write",
                spec_version="v1",
                schema_hash=schema_hash,
                resource_scope_resolver_id="different",
                resource_scope_resolver_version="v1",
            ),
            resource_scope_resolver=_Resolver(),
        )


def test_valid_arguments_reach_handler_and_return_five_outcomes() -> None:
    seen: list[object] = []

    def handler(arguments: object, _context: ToolContext) -> ToolResult:
        seen.append(arguments)
        return ToolResult.succeeded(CallId("call-1"), {"summary": "ok"})

    registry = ToolRegistry([FunctionTool(_spec(), handler)])
    call = ToolCall(
        call_id=CallId("call-1"),
        name="project_summary",
        arguments={"path": ".", "format": "short", "limit": 3, "tags": ["sdk"]},
    )

    result = asyncio.run(registry.invoke(call, _context()))

    assert result.outcome is ToolOutcome.SUCCEEDED
    assert seen == [{"path": ".", "format": "short", "limit": 3, "tags": ["sdk"]}]
    assert registry.calls[CallId("call-1")] is ToolCallState.SETTLED
    assert {
        ToolResult.succeeded(CallId("a")).outcome,
        ToolResult.partial(CallId("b"), {}).outcome,
        ToolResult.rejected(CallId("c"), "denied", "Denied.").outcome,
        ToolResult.failed(CallId("d"), "failed", "Failed.").outcome,
        ToolResult.unknown(CallId("e"), "Unknown.").outcome,
    } == set(ToolOutcome)


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": ".", "format": "short", "extra": True},
        {"path": ".", "format": "short", "limit": True},
        {"path": ".", "format": "medium"},
        {"path": "x" * 41, "format": "short"},
        {"path": ".", "format": "short", "tags": ["a", "b", "c"]},
        {"path": ".", "format": "short", "run-id": "forged"},
    ],
)
def test_malformed_arguments_are_rejected_before_handler(arguments: object) -> None:
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    registry = ToolRegistry([FunctionTool(_spec(), handler)])

    with pytest.raises(MalformedToolArgumentsError) as caught:
        asyncio.run(
            registry.invoke(
                ToolCall(CallId("call-1"), "project_summary", arguments),  # type: ignore[arg-type]
                _context(),
            )
        )

    assert calls == 0
    assert caught.value.code == "malformed_tool_arguments"


def test_schema_is_fail_closed_and_cannot_declare_reserved_fields() -> None:
    with pytest.raises(SchemaDefinitionError):
        ToolSpec(
            "unsafe",
            "Unsafe schema.",
            {
                "type": "object",
                "properties": {"authorization": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    with pytest.raises(SchemaDefinitionError):
        ToolSpec(
            "loose",
            "Loose schema.",
            {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        )
    with pytest.raises(SchemaDefinitionError):
        ToolSpec(
            "unknown-keyword",
            "Unknown schema keyword.",
            {
                "type": "object",
                "properties": {},
                "patternProperties": {},
                "additionalProperties": False,
            },
        )


def test_duplicate_call_id_never_invokes_handler_twice() -> None:
    calls = 0

    async def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("same"))

    async def scenario() -> None:
        registry = ToolRegistry([FunctionTool(_spec(), handler)])
        call = ToolCall(CallId("same"), "project_summary", {"path": ".", "format": "short"})
        assert (await registry.invoke(call, _context())).outcome is ToolOutcome.SUCCEEDED
        with pytest.raises(DuplicateToolCallError):
            await registry.invoke(call, _context())

    asyncio.run(scenario())
    assert calls == 1


def test_mismatched_or_late_result_is_rejected() -> None:
    registry = ToolRegistry(
        [
            FunctionTool(
                _spec(),
                lambda _arguments, _context: ToolResult.succeeded(CallId("wrong-call")),
            )
        ]
    )
    call = ToolCall(
        CallId("expected"),
        "project_summary",
        {"path": ".", "format": "short"},
    )

    with pytest.raises(LateToolResultError):
        asyncio.run(registry.invoke(call, _context()))


def test_cancelled_call_cancels_handler_and_keeps_stable_result() -> None:
    started = asyncio.Event()
    cancelled = False

    async def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def scenario() -> tuple[ToolResult, ToolCallState]:
        token = CancellationToken()
        registry = ToolRegistry([FunctionTool(_spec(), handler)])
        pending = asyncio.create_task(
            registry.invoke(
                ToolCall(
                    CallId("call-cancel"),
                    "project_summary",
                    {"path": ".", "format": "short"},
                ),
                _context(token),
            )
        )
        await started.wait()
        token.cancel()
        result = await pending
        return result, registry.calls[CallId("call-cancel")]

    result, state = asyncio.run(scenario())
    assert result == ToolResult.rejected(
        CallId("call-cancel"), "tool_cancelled", "Tool call was cancelled."
    )
    assert state is ToolCallState.CANCELLED
    assert cancelled is True


def test_public_contracts_are_immutable() -> None:
    result = ToolResult.succeeded(CallId("call-1"), {"ok": True})

    with pytest.raises(FrozenInstanceError):
        result.call_id = CallId("changed")  # type: ignore[misc]
    with pytest.raises(TypeError):
        _spec().input_schema["type"] = "array"  # type: ignore[index]


def test_no_shell_tool_is_registered_by_default() -> None:
    assert ToolRegistry().specs == ()
