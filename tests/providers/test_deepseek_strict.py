# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Scoped strict request projection against the original Gateway contract."""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from itertools import combinations
from typing import Any

import httpx
import pytest

from agent_orchestrator.runtime.tool_gateway import TOOL_SCHEMAS
from simple_harness.contracts import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderProtocolError,
    ProviderRequest,
    ProviderToolSpec,
    Secret,
)
from simple_harness.providers.deepseek_strict import (
    DEEPSEEK_STRICT_ADAPTER_KEY,
    DEEPSEEK_STRICT_TOOL_SCHEMA_MODE,
    compile_deepseek_strict_schema,
)
from simple_harness.providers.openai_compatible import openai_chat_request_payload
from simple_harness.tools.schema import (
    ArgumentsValidationError,
    SchemaDefinitionError,
    validate_arguments,
)


def _request() -> ProviderRequest:
    return ProviderRequest(
        RequestId("strict-gateway"),
        (Message(MessageRole.USER, "Call a workspace tool."),),
        tuple(
            ProviderToolSpec(name, "Use the documented original tool arguments.", schema)
            for name, schema in TOOL_SCHEMAS.items()
        ),
        max_output_tokens=1024,
    )


def _projected_accepts(value: Any, schema: dict[str, Any]) -> bool:
    """Tiny independent evaluator for the emitted subset (including root anyOf)."""
    declared = schema["type"]
    types = (declared,) if isinstance(declared, str) else declared
    matched = any({
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[kind]() for kind in types)
    if not matched or ("enum" in schema and value not in schema["enum"]):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "anyOf" in schema and not any(
        _projected_accepts(value, variant) for variant in schema["anyOf"]
    ):
        return False
    if value is None:
        return True
    if isinstance(value, dict):
        props = schema.get("properties")
        if props is not None:
            if not set(schema.get("required", [])) <= set(value):
                return False
            if schema.get("additionalProperties") is False and not set(value) <= set(props):
                return False
            return all(_projected_accepts(item, props[key]) for key, item in value.items())
    if isinstance(value, list):
        return (len(value) >= schema.get("minItems", 0)
                and len(value) <= schema.get("maxItems", float("inf"))
                and all(_projected_accepts(item, schema["items"]) for item in value))
    if isinstance(value, str):
        return "pattern" not in schema or re.search(schema["pattern"], value) is not None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf"))
    return True


def _assert_equivalent(name: str, candidate: dict[str, Any]) -> None:
    original = TOOL_SCHEMAS[name]
    projected = compile_deepseek_strict_schema(original)
    try:
        validate_arguments(candidate, original)
        allowed = True
    except ArgumentsValidationError:
        allowed = False
    assert _projected_accepts(candidate, projected) == allowed, (name, candidate)


def test_legacy_payload_remains_exact_and_explicit_legacy_matches_default() -> None:
    request = _request()
    expected = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Call a workspace tool."}],
        "tools": [{
            "type": "function",
            "function": {"name": name, "description": "Use the documented original tool arguments.",
                         "parameters": deepcopy(schema)},
        } for name, schema in TOOL_SCHEMAS.items()],
        "max_tokens": 1024,
    }
    default = openai_chat_request_payload(request, model="deepseek-flash")
    assert default == expected
    assert json.dumps(default, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    )
    assert openai_chat_request_payload(
        request, model="deepseek-flash", tool_schema_mode="legacy"
    ) == expected
    assert OpenAICompatibleProvider._payload_for_model(request, "deepseek-flash") == expected


@pytest.mark.parametrize("endpoint", [
    "https://api.deepseek.com/beta", "https://api.deepseek.com/beta/chat/completions",
    "https://api.deepseek.com/beta/", "https://api.deepseek.com/beta/chat/completions/",
])
def test_strict_endpoint_and_target_identity(endpoint: str) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            strict = OpenAICompatibleProvider(
                client, endpoint, "deepseek-flash", Secret("fixture"),
                tool_schema_mode=DEEPSEEK_STRICT_TOOL_SCHEMA_MODE,
            )
            legacy = OpenAICompatibleProvider(client, endpoint, "deepseek-flash", Secret("fixture"))
            assert strict.target.adapter_key == DEEPSEEK_STRICT_ADAPTER_KEY
            assert strict.target != legacy.target
            assert legacy.target.adapter_key == "openai-compatible.chat-completions.v1"
            assert strict.target.endpoint_identity == (
                "https://api.deepseek.com/beta/chat/completions"
            )
    asyncio.run(exercise())


@pytest.mark.parametrize("endpoint", [
    "https://api.deepseek.com", "https://api.deepseek.com/v1",
    "https://api.deepseek.com/beta/other", "https://api.deepseek.com:443/beta",
    "https://api.deepseek.com@evil.invalid/beta", "https://evil.invalid/beta",
    "http://api.deepseek.com/beta", "http://localhost/beta",
    "https://api.deepseek.com/beta?x=1",
])
def test_strict_rejects_nonofficial_endpoint(endpoint: str) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                OpenAICompatibleProvider(
                    client, endpoint, "deepseek-flash", Secret("fixture"),
                    tool_schema_mode=DEEPSEEK_STRICT_TOOL_SCHEMA_MODE,
                )
    asyncio.run(exercise())


def test_unknown_mode_rejected_by_constructor_and_both_serializers() -> None:
    request = _request()
    with pytest.raises(ValueError, match="tool_schema_mode"):
        openai_chat_request_payload(request, model="m", tool_schema_mode="future")
    with pytest.raises(ValueError, match="tool_schema_mode"):
        OpenAICompatibleProvider._payload_for_model(request, "m", tool_schema_mode="future")

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="tool_schema_mode"):
                OpenAICompatibleProvider(
                    client, "https://api.deepseek.com/beta", "m", Secret("fixture"),
                    tool_schema_mode="future",
                )
    asyncio.run(exercise())


def test_original_gateway_tools_preserve_names_metadata_and_optional_subsets() -> None:
    request = _request()
    body = openai_chat_request_payload(
        request, model="deepseek-flash", tool_schema_mode=DEEPSEEK_STRICT_TOOL_SCHEMA_MODE
    )
    assert [tool["function"]["name"] for tool in body["tools"]] == list(TOOL_SCHEMAS)
    assert all(tool["function"]["strict"] is True for tool in body["tools"])
    assert all(tool["function"]["description"] == "Use the documented original tool arguments."
               for tool in body["tools"])
    for name, source in TOOL_SCHEMAS.items():
        projected = compile_deepseek_strict_schema(source)
        assert body["tools"][list(TOOL_SCHEMAS).index(name)]["function"]["parameters"] == projected
        options = [
            key for key in source.get("properties", {}) if key not in source.get("required", [])
        ]
        if options:
            assert projected["type"] == "object"
            assert len(projected["anyOf"]) == 2 ** len(options)
            for variant in projected["anyOf"]:
                assert variant["additionalProperties"] is False
                assert set(variant["required"]) == set(variant["properties"])
            for count in range(len(options) + 1):
                for selected in combinations(options, count):
                    keys = set(source.get("required", [])) | set(selected)
                    assert sum(
                        set(variant["properties"]) == keys for variant in projected["anyOf"]
                    ) == 1
        else:
            assert "anyOf" not in projected
        assert projected.get("description") == source.get("description")

    read = TOOL_SCHEMAS["workspace_read_file"]
    assert compile_deepseek_strict_schema(read)["anyOf"][-1]["properties"]["expected_sha256"] == {
        "type": "string", "pattern": r"^[\s\S]{64,64}(?![\s\S])",
        "description": read["properties"]["expected_sha256"]["description"],
    }


def test_gateway_argument_boundaries_match_original_contract() -> None:
    read_option_values = {
        "offset": 0, "max_chars": 4096, "expected_sha256": "a" * 64,
    }
    read_subsets = [
        {"path": "x", **{key: read_option_values[key] for key in selected}}
        for count in range(4)
        for selected in combinations(read_option_values, count)
    ]
    samples = {
        "workspace_read_file": [
            {"path": "x", **optional}
            for optional in (
                {}, {"offset": 0}, {"max_chars": 1}, {"expected_sha256": "a" * 64},
                {"offset": 1, "max_chars": 4096, "expected_sha256": "a" * 64},
                {"offset": -1}, {"offset": True}, {"max_chars": 0}, {"max_chars": 4097},
                {"expected_sha256": "a" * 63}, {"expected_sha256": "a" * 65},
                {"expected_sha256": "a" * 63 + "\n"},
                {"expected_sha256": "a" * 64 + "\n"}, {"extra": 1},
                {"expected_sha256": "\n" + "a" * 63},
                {"expected_sha256": "a" * 63 + "\n\n"},
            )
        ] + read_subsets + [{}, {"path": 1}],
        "workspace_write_file": [
            {"path": "x", "content": ""}, {}, {"path": "x"}, {"content": "x"},
            {"path": "x", "content": "ok", "extra": True}, {"path": 1, "content": "x"},
        ],
        "workspace_list": [{}, {"path": "x"}, {"other": 1}],
        "run_tests": [{}, {"path": "tests"}, {"path": 1}, {"path": None}, {"extra": 1}],
    }
    for name, values in samples.items():
        for value in values:
            _assert_equivalent(name, value)


@pytest.mark.parametrize(("bounds", "values"), [
    ({"minLength": 2}, ("", "a", "a\n", "ab\n", "\n\n")),
    ({"maxLength": 2}, ("", "a\n", "ab", "ab\n", "a\n\n")),
    ({"minLength": 2, "maxLength": 2}, ("a", "a\n", "ab", "ab\n", "\n\n")),
])
def test_string_length_end_anchor_matches_trailing_newline_boundaries(
    bounds: dict[str, int], values: tuple[str, ...]
) -> None:
    source = {
        "type": "object", "additionalProperties": False,
        "properties": {"value": {"type": "string", **bounds}},
        "required": ["value"],
    }
    projected = compile_deepseek_strict_schema(source)
    for value in values:
        arguments = {"value": value}
        try:
            validate_arguments(arguments, source)
            expected = True
        except ArgumentsValidationError:
            expected = False
        assert _projected_accepts(arguments, projected) == expected, (bounds, value)


def test_nested_closed_projection_preserves_constraints_and_source_unchanged() -> None:
    source = {
        "type": "object", "description": "outer", "additionalProperties": False,
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 2},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                }, "required": ["name"],
            }},
        }, "required": ["items"],
    }
    before = deepcopy(source)
    projected = compile_deepseek_strict_schema(source)
    assert source == before
    assert "anyOf" not in projected
    for candidate in ({"items": [{"name": "a"}]}, {"items": [{"name": "ab", "score": 1}]},
                      {"items": [{"name": ""}]}, {"items": [{"name": "abc"}]},
                      {"items": [{"name": "a", "score": -0.1}]},
                      {"items": [{"name": "a", "extra": 0}]}, {"items": []}):
        try:
            validate_arguments(candidate, source)
            expected = True
        except ArgumentsValidationError:
            expected = False
        assert _projected_accepts(candidate, projected) == expected


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(additionalProperties=True),
    lambda s: s.pop("additionalProperties"),
    lambda s: s["properties"].update({"x": {"type": "object", "properties": {}}}),
    lambda s: s["properties"].update({f"opt{i}": {"type": "string"} for i in range(4)}),
    lambda s: s.update(pattern=".*"),
    lambda s: s["properties"].update({"x": {"type": "string", "pattern": ".*", "minLength": 1}}),
    lambda s: s.update(oneOf=[{"type": "object"}]),
    lambda s: s["properties"].update({"x": {"type": "mystery"}}),
    lambda s: s["properties"].update({"x": {"type": ["string", "null"]}}),
    lambda s: s["properties"].update({"x": {"type": "null"}}),
    lambda s: s["properties"].update({
        "x": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    }),
    lambda s: s["properties"].update({
        "x": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    }),
    lambda s: s["properties"].update({
        "x": {"type": ["object", "null"], "additionalProperties": False},
    }),
])
def test_unsupported_or_conflicting_schema_rejected(mutation: Any) -> None:
    source: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
    mutation(source)
    with pytest.raises(SchemaDefinitionError):
        compile_deepseek_strict_schema(source)


def test_expansion_has_total_node_bound() -> None:
    node: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
    for _ in range(5):
        node = {"type": "object", "additionalProperties": False,
                "properties": {"child": node, **{f"opt{i}": {"type": "boolean"} for i in range(3)}},
                "required": ["child"]}
    with pytest.raises(SchemaDefinitionError, match="limit exceeded"):
        compile_deepseek_strict_schema(node)


def test_http_body_is_shared_serializer_and_bad_tool_args_keep_usage() -> None:
    request = _request()
    sent: list[dict[str, Any]] = []

    def handler(wire: httpx.Request) -> httpx.Response:
        assert str(wire.url) == "https://api.deepseek.com/beta/chat/completions"
        sent.append(json.loads(wire.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-bad", "type": "function", "function": {
                        "name": "run_tests", "arguments": '{"path":',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        })

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://api.deepseek.com/beta", "deepseek-flash", Secret("fixture"),
                tool_schema_mode=DEEPSEEK_STRICT_TOOL_SCHEMA_MODE,
            )
            with pytest.raises(ProviderProtocolError) as caught:
                await provider.invoke(request, cancel=CancelToken())
            assert caught.value.detail == {
                "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16,
                          "cache_tokens": None, "reasoning_tokens": None},
                "finish_reason": "tool_calls", "parse_stage": "tool_parse",
                "tool_parse_reason": "arguments_json",
            }
    asyncio.run(exercise())
    assert sent == [openai_chat_request_payload(
        request, model="deepseek-flash", tool_schema_mode=DEEPSEEK_STRICT_TOOL_SCHEMA_MODE
    )]


def test_strict_response_keeps_original_tool_names_and_arguments() -> None:
    read_args = {"path": "x", "offset": 1, "max_chars": 4096,
                 "expected_sha256": "a" * 64}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": f"call-{index}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                } for index, (name, arguments) in enumerate((
                    ("run_tests", {}), ("workspace_read_file", read_args),
                ))],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://api.deepseek.com/beta", "deepseek-flash", Secret("fixture"),
                tool_schema_mode=DEEPSEEK_STRICT_TOOL_SCHEMA_MODE,
            )
            result = await provider.invoke(_request(), cancel=CancelToken())
            assert [(call.name, dict(call.arguments)) for call in result.tool_calls] == [
                ("run_tests", {}), ("workspace_read_file", read_args),
            ]
            assert result.usage is not None and result.usage.total_tokens == 15

    asyncio.run(exercise())
