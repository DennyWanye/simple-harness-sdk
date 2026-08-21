# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Minimal Consumer Example - Main Entry Point

Demonstrates basic Simple Harness SDK integration.

Exit code contract:
- 0: the run reached the declared terminal state (RunState.COMPLETED)
- 1: the run did NOT reach the declared terminal state
"""

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

from simple_harness.runtime import (
    build_consumer_runtime,
    ConsumerRuntimePorts,
    RunStart,
    RunClient,
)
from simple_harness.execution.uow import RunState
from simple_harness.contracts import ExecutionSessionId, RunId, RequestId

from ports.provider import MockLLMProvider
from ports.tools import CalculatorToolExecutor
from ports.auth import AlwaysAllowAuthorization

# The terminal state this demo declares as success. Anything else exits non-zero.
EXPECTED_TERMINAL_STATE = RunState.COMPLETED


async def main() -> int:
    """Run a simple agent task. Returns process exit code."""

    print("=== Minimal Consumer Example ===\n")

    # Fresh database in a temp directory on every invocation, so the example
    # is re-runnable without colliding with persisted execution state.
    db_path = Path(tempfile.mkdtemp(prefix="minimal-consumer-")) / "execution.db"
    print(f"Using database: {db_path}")

    # Build consumer runtime ports
    print("Building runtime...")
    ports = ConsumerRuntimePorts(
        provider=MockLLMProvider(),
        tool_executor=CalculatorToolExecutor(),
        authorization=AlwaysAllowAuthorization(),
        database_path=str(db_path),
        tool_names=("calculate", "echo"),
        max_turns=10,
        max_tool_calls=20,
    )

    runtime = await build_consumer_runtime(ports)

    try:
        # Enter runtime context
        await runtime.__aenter__()

        # Create run client
        client = RunClient(runtime)

        # Fresh IDs on every invocation (persisted runs are keyed on them)
        suffix = uuid.uuid4().hex[:8]
        run_id = RunId(f"run-{suffix}")
        run_start = RunStart(
            execution_session_id=ExecutionSessionId(f"session-{suffix}"),
            run_id=run_id,
            request_id=RequestId(f"req-{suffix}"),
            turn_id="turn-001",
            tool_catalog_generation=1,
            input={
                "messages": [
                    {"role": "user", "content": "What is 2 + 2?"}
                ],
                "capability_snapshot": {
                    "tools": ["calculate", "echo"],
                },
                # Required: without an output-token bound the kernel cannot
                # price the provider reservation, treats the charge as unknown,
                # and the ReAct driver fails the run with react_cost_exceeded.
                "max_output_tokens": 1024,
            },
        )

        print(f"\n[Runtime] Starting run {run_id}")
        print(f"[User] What is 2 + 2?\n")

        # Start run
        await client.start(run_start)

        # Wait for completion. NOTE: wait_idle() returns None — it only
        # signals that the run is no longer live. The terminal state must be
        # read back from the execution store via client.query(run_id).
        print("[Agent] Processing...")
        await runtime.wait_idle(run_id)

        record = client.query(run_id)
        state = record.state if record is not None else None

        print(f"\n[Runtime] Run terminal state: {state}\n")

        if state == EXPECTED_TERMINAL_STATE:
            print("✅ Task completed successfully")
            print("\nIn a real app, you would:")
            print("  1. Fetch final state from database")
            print("  2. Extract terminal_payload")
            print("  3. Show result to user")
            return 0

        print(f"❌ FAIL: expected terminal state {EXPECTED_TERMINAL_STATE}, got {state}")
        return 1

    finally:
        # Cleanup
        print("\nCleaning up...")
        await runtime.__aexit__(None, None, None)
        print("Done!")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
