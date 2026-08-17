"""Mock LLM Provider for demonstration purposes.

In a real application, replace this with actual LLM API client.
"""

import asyncio
import json
import uuid
from typing import Optional

from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    ProviderToolCall,
)


class MockLLMProvider:
    """Mock provider that simulates tool-calling workflow."""

    def __init__(self):
        self.call_count = 0

    async def invoke(
        self,
        request: ProviderRequest,
        *,
        cancel,
    ) -> ProviderResponse:
        """Simulate LLM response with tool calling."""

        self.call_count += 1

        # Simulate network latency
        await asyncio.sleep(0.1)

        # Parse last message
        last_message = request.messages[-1]

        # First call: Use tool
        if self.call_count == 1 and last_message.role.value == "user":
            print("[Agent] Thinking... (using calculate tool)")
            return ProviderResponse(
                request_id=request.request_id,
                content=None,
                tool_calls=(
                    ProviderToolCall(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        name="calculate",
                        arguments=json.dumps({"expression": "2+2"}),
                    ),
                ),
                usage=ProviderUsage(
                    prompt_tokens=50,
                    completion_tokens=20,
                ),
                finish_reason="tool_calls",
            )

        # Second call: Final answer
        elif last_message.role.value == "tool":
            print("[Agent] Formulating final answer...")
            return ProviderResponse(
                request_id=request.request_id,
                content="The answer is 4. I calculated 2 + 2 using the calculator tool.",
                tool_calls=(),
                usage=ProviderUsage(
                    prompt_tokens=80,
                    completion_tokens=30,
                ),
                finish_reason="stop",
            )

        # Fallback
        else:
            return ProviderResponse(
                request_id=request.request_id,
                content="I understand.",
                tool_calls=(),
                usage=ProviderUsage(
                    prompt_tokens=10,
                    completion_tokens=5,
                ),
                finish_reason="stop",
            )
