# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

import httpx

from simple_harness.contracts.identity import CallId, RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderRequest,
    ProviderToolSpec,
    Secret,
)


class _Handler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers["Authorization"],
                "body": body,
            }
        )
        payload = {
            "id": "completion-local-1",
            "model": "local-model",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-local-1",
                                "type": "function",
                                "function": {
                                    "name": "project_summary_read",
                                    "arguments": json.dumps({"section": "tests"}),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("x-request-id", "http-request-local-1")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _server() -> Iterator[tuple[ThreadingHTTPServer, str]]:
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_real_http_round_trip_preserves_structured_tools_and_usage() -> None:
    async def exercise(base_url: str):
        async with httpx.AsyncClient() as client:
            provider = OpenAICompatibleProvider(
                client, base_url, "local-model", Secret("local-secret"), timeout=2
            )
            return await provider.invoke(
                ProviderRequest(
                    request_id=RequestId("request-local-1"),
                    messages=(
                        Message(role=MessageRole.USER, content="read summary"),
                        Message(
                            role=MessageRole.TOOL,
                            content='{"ok":true}',
                            name="project_summary_read",
                            call_id=CallId("previous-call"),
                        ),
                    ),
                    tools=(
                        ProviderToolSpec(
                            name="project_summary_read",
                            description="Read project summary",
                            parameters={
                                "type": "object",
                                "properties": {"section": {"type": "string"}},
                                "additionalProperties": False,
                            },
                        ),
                    ),
                    temperature=0,
                    max_output_tokens=128,
                ),
                cancel=CancelToken(),
            )

    with _server() as (_, base_url):
        result = asyncio.run(exercise(base_url))

    assert len(_Handler.requests) == 1
    captured = _Handler.requests[0]
    assert captured["path"] == "/v1/chat/completions"
    assert captured["authorization"] == "Bearer local-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "local-model"
    assert body["messages"][1]["tool_call_id"] == "previous-call"
    assert body["tools"][0]["function"]["name"] == "project_summary_read"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 128
    assert result.tool_calls[0].name == "project_summary_read"
    assert dict(result.tool_calls[0].arguments) == {"section": "tests"}
    assert result.usage is not None and result.usage.total_tokens == 13
    assert result.provider_request_id == "http-request-local-1"
