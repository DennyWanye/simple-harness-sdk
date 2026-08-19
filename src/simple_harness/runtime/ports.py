# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Consumer-facing port interfaces for Simple Harness SDK integration.

These Protocol classes define the minimal interfaces consumers must implement
to integrate the SDK. The SDK provides adapters that bridge these simple
consumer ports to the internal kernel ports.
"""

from __future__ import annotations

from typing import Any, Protocol

from simple_harness.contracts import JsonValue
from simple_harness.providers import ProviderRequest, ProviderResponse
from simple_harness.tools import ToolCall, ToolResult


class ProviderPort(Protocol):
    """LLM provider interface for consumers.

    Consumers implement this to connect their LLM service (OpenAI, Anthropic, etc.)
    to the Runtime. The SDK handles request coordination, error recovery, and
    provider reconciliation.

    The returned ``ProviderResponse.model`` MUST echo the model declared in
    ``ConsumerRuntimePorts.model``. The kernel only trusts reported usage when
    ``response.model == target.model``; a mismatch records an unknown charge,
    which refuses the run on a later turn.

    Example implementation:
        class MyOpenAIProvider:
            async def invoke(self, request, *, cancel):
                response = await httpx.post(
                    "https://api.openai.com/v1/chat/completions",
                    json={
                        "model": request.model,
                        "messages": [{"role": m.role.value, "content": m.content}
                                     for m in request.messages],
                    },
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                data = response.json()
                return ProviderResponse(
                    request_id=request.request_id,
                    message=Message(
                        MessageRole.ASSISTANT,
                        data["choices"][0]["message"]["content"],
                    ),
                    tool_calls=(),
                    usage=ProviderUsage(
                        input_tokens=data["usage"]["prompt_tokens"],
                        output_tokens=data["usage"]["completion_tokens"],
                    ),
                    model=request.model,
                    finish_reason=data["choices"][0]["finish_reason"],
                )
    """

    async def invoke(
        self,
        request: ProviderRequest,
        *,
        cancel,
    ) -> ProviderResponse:
        """Execute a single LLM request and return the response.

        Args:
            request: Provider request containing model, messages, tools, etc.
            cancel: Cancellation token (check `cancel.cancelled` periodically)

        Returns:
            ProviderResponse with message/tool_calls and usage statistics; the
            ``model`` field must echo the model declared by the consumer.

        Raises:
            ProviderTransportError: Network/server errors
            ProviderAuthenticationError: Invalid credentials
            ProviderRateLimitError: Rate limit exceeded
            Other ProviderError subclasses for specific failures

        Constraints:
            - Must handle tool calling if request.tools is provided
            - Must redact secrets in error messages
            - Should check cancellation token during long operations
        """
        ...


class ToolExecutorPort(Protocol):
    """Tool execution interface for consumers.

    Consumers implement this to provide tools (file I/O, web search, databases, etc.)
    to the Agent. The SDK handles tool orchestration, authorization, and reconciliation.

    Example implementation:
        class MyToolExecutor:
            async def execute(self, call, context):
                if call.name == "read_file":
                    path = call.arguments["path"]
                    content = open(path).read()
                    return ToolResult.succeeded(call.call_id, {"content": content})
                else:
                    return ToolResult.rejected(
                        call.call_id, "unknown_tool", "Tool not found"
                    )
    """

    async def execute(
        self,
        call: ToolCall,
        context: dict[str, Any],
    ) -> ToolResult:
        """Execute a tool call and return the result.

        Args:
            call: Tool call with name, arguments, and call_id
            context: Execution context (session_id, run_id, etc.)

        Returns:
            ToolResult with outcome (succeeded/failed/rejected/unknown)

        Constraints:
            - Use ToolResult.succeeded() for successful execution
            - Use ToolResult.failed() for known errors (file not found, etc.)
            - Use ToolResult.rejected() for invalid calls (auth denied, etc.)
            - Use ToolResult.unknown() when side effects might have happened
            - Must validate arguments before execution
        """
        ...


class AuthorizationRequest:
    """Request for user authorization to execute a tool.

    Attributes:
        tool_call: The tool call awaiting authorization
        run_id: Execution run identifier
        risk_level: "low" | "medium" | "high" (optional)
    """

    def __init__(self, tool_call: ToolCall, run_id: str, risk_level: str | None = None):
        self.tool_call = tool_call
        self.run_id = run_id
        self.risk_level = risk_level


class AuthorizationResult:
    """Result of an authorization request.

    Use the factory methods:
    - AuthorizationResult.allow() - Approve the tool call
    - AuthorizationResult.deny(reason) - Reject the tool call
    - AuthorizationResult.defer(reason) - User did not respond
    """

    def __init__(self, decision: str, reason: str | None = None):
        self.decision = decision  # "allow" | "deny" | "defer"
        self.reason = reason

    @classmethod
    def allow(cls):
        """Approve the tool call."""
        return cls("allow")

    @classmethod
    def deny(cls, reason: str):
        """Reject the tool call with a reason."""
        return cls("deny", reason)

    @classmethod
    def defer(cls, reason: str):
        """User did not respond or deferred decision."""
        return cls("defer", reason)


class AuthorizationPort(Protocol):
    """Authorization interface for consumers.

    Consumers implement this to request user approval for sensitive tool calls.
    The SDK handles authorization timing, retries, and enforcement.

    Example implementation:
        class MyAuthorization:
            async def request_authorization(self, request):
                # Show UI dialog to user
                decision = await self.ui.show_permission_dialog(
                    tool_name=request.tool_call.name,
                    arguments=request.tool_call.arguments,
                )
                if decision == "allow":
                    return AuthorizationResult.allow()
                else:
                    return AuthorizationResult.deny("User denied permission")
    """

    async def request_authorization(
        self,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        """Request user authorization for a tool call.

        Args:
            request: Authorization request with tool_call, run_id, risk_level

        Returns:
            AuthorizationResult with decision (allow/deny/defer)

        Constraints:
            - Should return quickly (user has limited time to respond)
            - Use defer() if user doesn't respond in time
            - Should show enough context for user to make informed decision
        """
        ...


class MemoryQueryPort(Protocol):
    """Read-only memory recall interface.

    reserved — declared but not yet wired into Runtime; do not assume recall is
    active. The Runtime does not currently call this port.

    Consumers implement this to give the Agent access to long-term memory
    without owning the storage. The Runtime calls this port when an Agent
    needs to recall relevant information from past conversations or knowledge.

    Example implementation:
        class MyMemoryQuery:
            async def recall_readonly(self, query, limit, scope):
                # Search your memory database
                results = await self.db.search(query, limit=limit, scope=scope)
                return [{"content": r.text, "timestamp": r.ts} for r in results]
    """

    async def recall_readonly(
        self,
        query: str,
        limit: int,
        scope: str,
    ) -> list[dict[str, JsonValue]]:
        """Return at most `limit` memory entries relevant to `query` within `scope`.

        Args:
            query: Natural language query describing what to recall
            limit: Maximum number of entries to return
            scope: Memory scope identifier (e.g., "user:123", "session:abc")

        Returns:
            List of memory entries as JSON-safe dictionaries. Each entry should
            contain at least a "content" field. Common fields include:
            - content: The memory text
            - timestamp: When the memory was created
            - relevance: Optional relevance score

        Raises:
            May raise exceptions on database errors. The Runtime will log and
            continue without memory augmentation.

        Constraints:
            - Must never write or mutate any state
            - Should return quickly (< 1 second for typical queries)
            - Empty list if no relevant memories found
        """
        ...


class MemoryWritePort(Protocol):
    """Write interface for session-scoped working memory.

    reserved — declared but not yet wired into Runtime; do not assume working
    memory is active. The Runtime does not currently call this port.

    Consumers implement this to persist short-term notes and todos across turns.
    The Runtime calls this port when an Agent updates its working memory list.

    Example implementation:
        class MyMemoryWrite:
            async def replace_session_todos(self, session_id, items):
                # Overwrite the full todo list for this session
                await self.db.execute(
                    "DELETE FROM todos WHERE session_id = ?", (session_id,)
                )
                for item in items:
                    await self.db.execute(
                        "INSERT INTO todos (session_id, content) VALUES (?, ?)",
                        (session_id, item["content"])
                    )
    """

    async def replace_session_todos(
        self,
        session_id: str,
        items: list[dict[str, JsonValue]],
    ) -> None:
        """Replace the full working-memory list for `session_id`.

        Args:
            session_id: Execution session identifier
            items: New todo/note list as JSON-safe dictionaries. Each item
                   typically contains a "content" field describing the note.

        Raises:
            May raise exceptions on database errors. The Runtime will log the
            error and continue (working memory is non-critical).

        Constraints:
            - Should replace the entire list atomically (not append)
            - Previous items for this session should be removed
            - Empty list means clear all todos
            - Should complete quickly (< 500ms typical)
        """
        ...


__all__ = (
    "ProviderPort",
    "ToolExecutorPort",
    "AuthorizationPort",
    "AuthorizationRequest",
    "AuthorizationResult",
    "MemoryQueryPort",
    "MemoryWritePort",
)
