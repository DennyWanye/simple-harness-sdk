# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The model's skill_execute never blocks the event loop on a script (RP-E3 leftover 1).

A runner with ``run_async`` (the Host sandbox executor is asynchronous) is awaited; a
synchronous runner is moved to a worker thread.  Either way the loop keeps serving.
"""

from __future__ import annotations

import asyncio
import json
import threading

from arp_fixture import build
from provider_fixture import ScriptedProvider
from skill_fixture import AcceptingAssurance, admit_skill, import_skill, receipt
from test_arp_skill_use import SKILL_EXECUTE_TOOL_NAME, _agent, _context, _script_bundle, _tools

from simple_harness.agents.arp.strict import plain


class AsyncRunner:
    def __init__(self) -> None:
        self.ticks_during_run = 0

    def run(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("the async path must be used inside the loop")

    async def run_async(self, request):  # type: ignore[no-untyped-def]
        for _ in range(3):
            await asyncio.sleep(0.01)
        return receipt(output=b'{"n": 2}')


class BlockingRunner:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None

    def run(self, request):  # type: ignore[no-untyped-def]
        self.thread = threading.current_thread()
        import time

        time.sleep(0.1)
        return receipt(output=b'{"n": 2}')


def _execute(runner, tmp_path):  # type: ignore[no-untyped-def]
    async def case() -> tuple[dict, int]:
        runtime = build(tmp_path, ScriptedProvider(["好的。"]), acceptance=AcceptingAssurance(), script_runner=runner)
        async with runtime:
            revision = import_skill(runtime, _script_bundle(runtime), command="i1", fmt="NATIVE").revision
            admit_skill(runtime, revision, command="c1")
            agent = await _agent(runtime)
            execute = _tools(runtime)[SKILL_EXECUTE_TOOL_NAME]
            ticks = 0
            stop = asyncio.Event()

            async def ticker() -> None:
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            result = await execute.invoke({"skill_ref": revision.pin.to_json(), "arguments": {"n": 1}}, _context(agent, 1))
            stop.set()
            await task
            assert result.outcome.value == "succeeded", result
            return plain(result.value), ticks

    return asyncio.run(case())


def test_async_runner_is_awaited(tmp_path) -> None:
    view, ticks = _execute(AsyncRunner(), tmp_path)
    assert json.loads(view["inline_text"]) == {"n": 2} and ticks >= 2


def test_sync_runner_runs_off_the_loop(tmp_path) -> None:
    runner = BlockingRunner()
    view, ticks = _execute(runner, tmp_path)
    assert json.loads(view["inline_text"]) == {"n": 2}
    assert runner.thread is not threading.main_thread() and ticks >= 5
