# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect

import pytest

from simple_harness.testing import arm64_candidate
from simple_harness.testing.arm64_candidate import (
    Arm64CandidateGateError,
    run_core_gate,
)


def test_arm64_core_gate_has_frozen_synchronous_public_signature() -> None:
    assert tuple(inspect.signature(run_core_gate).parameters) == ()
    assert not inspect.iscoroutinefunction(run_core_gate)
    assert arm64_candidate.__all__ == (
        "Arm64CandidateGateError",
        "run_core_gate",
    )
    source = inspect.getsource(arm64_candidate)
    assert '_DEFAULT_MEMORY_VERSION = "0.5.0"' in source
    assert "SIMPLE_HARNESS_MEMORY_CANDIDATE_VERSION" in source
    assert 'getattr(root, "MemoryManager", None)' in source
    assert "ConversationMemoryAdapter" not in source


def test_arm64_core_gate_fails_closed_before_candidate_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(arm64_candidate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(arm64_candidate.platform, "machine", lambda: "x86_64")

    with pytest.raises(Arm64CandidateGateError) as captured:
        run_core_gate()
    assert captured.value.code == "arm64-architecture-required"
    assert str(captured.value) == "arm64-architecture-required"


def test_arm64_core_gate_cli_returns_stable_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(arm64_candidate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(arm64_candidate.platform, "machine", lambda: "x86_64")

    assert arm64_candidate.main() == 1
    assert capsys.readouterr().out == "arm64-architecture-required\n"
