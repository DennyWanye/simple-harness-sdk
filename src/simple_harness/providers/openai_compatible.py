# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Thin OpenAI-compatible HTTPS adapter with no retry or runtime state."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from simple_harness.contracts.identity import CallId
from simple_harness.contracts.json import JsonValue, validate_json_value
from simple_harness.contracts.messages import Message, MessageRole

from .base import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
    ProviderUsage,
    Secret,
)
from .errors import (
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderPaymentRequiredError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from .redaction import SecretRedactor


def _json_value(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    validate_json_value(value)
    return value


def _plain_mapping(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: _json_value(item) for key, item in value.items()}


class OpenAICompatibleProvider:
    """Perform one OpenAI-compatible chat-completions request per invocation."""

    __slots__ = ("_client", "_endpoint", "_redactor", "_secret", "_target", "_timeout")

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        secret: Secret,
        timeout: float | httpx.Timeout = 30.0,
        *,
        provider_id: str | None = None,
        pricing_key: str | None = None,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("client must be an httpx.AsyncClient")
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("base_url must be a valid absolute HTTP(S) URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.scheme == "http" and hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("non-loopback provider URLs must use HTTPS")
        if not model.strip():
            raise ValueError("model must not be blank")
        normalized = base_url.rstrip("/")
        self._endpoint = (
            normalized
            if normalized.endswith("/chat/completions")
            else normalized + "/chat/completions"
        )
        self._client = client
        self._secret = secret
        if isinstance(timeout, bool) or (
            isinstance(timeout, (int, float)) and timeout <= 0
        ):
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._redactor = SecretRedactor.from_secrets(secret)
        self._target = ProviderTarget(
            provider_id=provider_id or str(hostname),
            model=model,
            pricing_key=pricing_key or model,
            endpoint_identity=self._endpoint,
            adapter_key="openai-compatible.chat-completions.v1",
        )

    @property
    def target(self) -> ProviderTarget:
        return self._target

    async def invoke(
        self, request: ProviderRequest, *, cancel: CancelToken
    ) -> ProviderResponse:
        if cancel.is_cancelled:
            raise ProviderCancelledError()

        request_task = asyncio.create_task(self._post_once(request))
        cancel_task = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if request_task in done:
                return await request_task
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
            raise ProviderCancelledError()
        except asyncio.CancelledError:
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
            raise ProviderCancelledError() from None
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _post_once(self, request: ProviderRequest) -> ProviderResponse:
        try:
            response = await self._client.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._secret.reveal()}",
                    "Content-Type": "application/json",
                },
                json=self._request_payload(request),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                private_cause=self._redactor.exception(exc)
            ) from None
        except httpx.RequestError as exc:
            raise ProviderTransportError(
                private_cause=self._redactor.exception(exc)
            ) from None

        self._raise_for_status(response.status_code)
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError(
                private_cause=RuntimeError("response body was not valid JSON")
            ) from None
        return self._parse_response(request, payload, response)

    def _request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._target.model,
            "messages": [
                self._message_payload(message) for message in request.messages
            ],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _plain_mapping(tool.parameters),
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    @staticmethod
    def _message_payload(message: Message) -> dict[str, Any]:
        role = (
            message.role.value
            if isinstance(message.role, MessageRole)
            else str(message.role)
        )
        payload: dict[str, Any] = {
            "role": role,
            "content": (
                message.content
                if isinstance(message.content, str)
                else [block.to_dict() for block in message.content]
            ),
        }
        if message.name is not None:
            payload["name"] = message.name
        if message.call_id is not None:
            payload["tool_call_id"] = message.call_id.value
        return payload

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if 200 <= status_code < 300:
            return
        if status_code == 401:
            raise ProviderAuthenticationError(status_code=status_code)
        if status_code == 402:
            raise ProviderPaymentRequiredError(status_code=status_code)
        if status_code == 408:
            raise ProviderTimeoutError(status_code=status_code)
        if status_code == 429:
            raise ProviderRateLimitError(status_code=status_code)
        if 500 <= status_code < 600:
            raise ProviderServerError(status_code=status_code)
        raise ProviderRequestRejectedError(status_code=status_code)

    def _parse_response(
        self,
        request: ProviderRequest,
        payload: Any,
        response: httpx.Response,
    ) -> ProviderResponse:
        if not isinstance(payload, Mapping):
            raise ProviderProtocolError()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderProtocolError()
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderProtocolError()
        raw_message = choice.get("message")
        if not isinstance(raw_message, Mapping):
            raise ProviderProtocolError()
        content = raw_message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ProviderProtocolError()

        tool_calls = self._parse_tool_calls(raw_message.get("tool_calls", []))
        usage = self._parse_usage(payload.get("usage"))
        model = payload.get("model")
        if model is None:
            model = self._target.model
        finish_reason = choice.get("finish_reason")
        provider_request_id = response.headers.get("x-request-id") or payload.get("id")
        for field_value in (model, finish_reason, provider_request_id):
            if field_value is not None and not isinstance(field_value, str):
                raise ProviderProtocolError()
        return ProviderResponse(
            request_id=request.request_id,
            message=Message(role=MessageRole.ASSISTANT, content=content),
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=finish_reason,
            provider_request_id=provider_request_id,
        )

    @staticmethod
    def _parse_tool_calls(raw_calls: Any) -> tuple[ProviderToolCall, ...]:
        if raw_calls is None:
            return ()
        if not isinstance(raw_calls, list):
            raise ProviderProtocolError()
        parsed: list[ProviderToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping) or raw_call.get("type") != "function":
                raise ProviderProtocolError()
            raw_id = raw_call.get("id")
            function = raw_call.get("function")
            if (
                not isinstance(raw_id, str)
                or not raw_id
                or not isinstance(function, Mapping)
            ):
                raise ProviderProtocolError()
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not name:
                raise ProviderProtocolError()
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ProviderProtocolError() from None
            if not isinstance(arguments, Mapping):
                raise ProviderProtocolError()
            try:
                normalized = _plain_mapping(arguments)
                validate_json_value(normalized)
                parsed.append(
                    ProviderToolCall(
                        call_id=CallId(raw_id), name=name, arguments=normalized
                    )
                )
            except (TypeError, ValueError):
                raise ProviderProtocolError() from None
        return tuple(parsed)

    @staticmethod
    def _parse_usage(raw_usage: Any) -> ProviderUsage | None:
        if raw_usage is None:
            return None
        if not isinstance(raw_usage, Mapping):
            raise ProviderProtocolError()
        prompt = raw_usage.get("prompt_tokens")
        completion = raw_usage.get("completion_tokens")
        total = raw_usage.get("total_tokens")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (prompt, completion, total)
        ):
            raise ProviderProtocolError()
        try:
            prompt_details = raw_usage.get("prompt_tokens_details")
            completion_details = raw_usage.get("completion_tokens_details")
            cache_tokens = (
                prompt_details.get("cached_tokens")
                if isinstance(prompt_details, Mapping)
                else None
            )
            reasoning_tokens = (
                completion_details.get("reasoning_tokens")
                if isinstance(completion_details, Mapping)
                else None
            )
            return ProviderUsage(
                prompt,
                completion,
                total,
                cache_tokens=cache_tokens,
                reasoning_tokens=reasoning_tokens,
            )
        except ValueError:
            raise ProviderProtocolError() from None
