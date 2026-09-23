"""Offline bounds for the opt-in real-provider WAIT resume runner."""

from __future__ import annotations

import asyncio
import importlib.util
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "acceptance" / "resume_real_deepseek_wait.py"
)
_SPEC = importlib.util.spec_from_file_location("resume_real_deepseek_wait", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)

SerialRealProvider = _RUNNER.SerialRealProvider
_exit_code = _RUNNER._exit_code
_resume_output_path = _RUNNER._resume_output_path
_resume_permitted = _RUNNER._resume_permitted


def test_resume_runner_cap_parks_without_a_second_delegate_call() -> None:
    class Delegate:
        calls = 0

        async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
            self.calls += 1
            return object()

    async def case() -> None:
        delegate = Delegate()
        provider = SerialRealProvider(delegate, max_calls=1)
        request = SimpleNamespace(messages=())
        await provider.invoke(request, cancel=None)
        blocked = asyncio.create_task(provider.invoke(request, cancel=None))
        await asyncio.wait_for(provider.cap_reached.wait(), timeout=1)
        assert delegate.calls == provider.physical_calls == 1
        blocked.cancel()
        with suppress(asyncio.CancelledError):
            await blocked

    asyncio.run(case())


def test_resume_runner_failed_terminal_is_not_success_and_outputs_are_unique(tmp_path) -> None:
    assert _exit_code("TERMINAL", "COMPLETED") == 0
    assert _exit_code("TERMINAL", "FAILED") == 1
    assert _exit_code("PARTIAL", "ACTIVE") == 1
    assert _resume_output_path(tmp_path) != _resume_output_path(tmp_path)


def test_resume_runner_allows_an_existing_pending_root_review_without_a_new_grant() -> None:
    assert _resume_permitted(
        [],
        [],
        [
            {
                "intent_id": "root-review-2",
                "prompt_version": "root-reviewer-v4",
                "state": "SUBMITTED",
            }
        ],
    )
