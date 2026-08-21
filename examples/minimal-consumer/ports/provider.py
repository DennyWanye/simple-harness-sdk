# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Mock LLM Provider for demonstration purposes.

In a real application, replace this with actual LLM API client.
"""

import asyncio
import uuid

from simple_harness.contracts import CallId, Message, MessageRole
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderUsage,
)

# The 0.1.2 consumer adapter pins ProviderTarget(model="consumer-model").
# The kernel only trusts reported usage (instead of recording an
# unknown charge that trips the react_cost_exceeded guard) when the
# response model matches the target model, so the mock echoes it here.
ADAPTER_MODEL = "consumer-model"


class MockLLMProvider:
    """Mock provider that simulates a tool-calling workflow."""

    def __init__(self):
        self.call_count = 0

    async def invoke(
        self,
        request: ProviderRequest,
        *,
        cancel,
    ) -> ProviderResponse:
        """Simulate an LLM response with tool calling."""

        self.call_count += 1

        # Simulate network latency
        await asyncio.sleep(0.1)

        # Parse last message
        last_message = request.messages[-1]

        # First call: request the calculate tool
        if self.call_count == 1 and last_message.role == MessageRole.USER:
            print("[Agent] Thinking... (using calculate tool)")
            return ProviderResponse(
                request_id=request.request_id,
                message=Message(MessageRole.ASSISTANT, ""),
                tool_calls=(
                    ProviderToolCall(
                        CallId(f"call-{uuid.uuid4().hex[:8]}"),
                        "calculate",
                        # The 0.1.2 consumer adapter registers placeholder tool
                        # specs ({"properties": {}, "additionalProperties": false}),
                        # so any non-empty argument mapping fails kernel-side
                        # schema validation. The demo therefore sends no
                        # arguments; the tool executor applies its default.
                        {},
                    ),
                ),
                usage=ProviderUsage(
                    input_tokens=50,
                    output_tokens=20,
                    total_tokens=70,
                ),
                model=ADAPTER_MODEL,
                finish_reason="tool_calls",
            )

        # Second call (after the tool result came back): final answer
        elif last_message.role == MessageRole.TOOL:
            print("[Agent] Formulating final answer...")
            return ProviderResponse(
                request_id=request.request_id,
                message=Message(
                    MessageRole.ASSISTANT,
                    "The answer is 4. I calculated 2 + 2 using the calculator tool.",
                ),
                tool_calls=(),
                usage=ProviderUsage(
                    input_tokens=80,
                    output_tokens=30,
                    total_tokens=110,
                ),
                model=ADAPTER_MODEL,
                finish_reason="stop",
            )

        # Fallback
        else:
            return ProviderResponse(
                request_id=request.request_id,
                message=Message(MessageRole.ASSISTANT, "I understand."),
                tool_calls=(),
                usage=ProviderUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
                model=ADAPTER_MODEL,
                finish_reason="stop",
            )
