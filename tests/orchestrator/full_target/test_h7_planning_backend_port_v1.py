from __future__ import annotations

import pytest
from dataclasses import replace
from types import SimpleNamespace

from agent_orchestrator.contracts.htn import SemanticReadSet
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.planning.htn.backend_port import (
    BackendStatus,
    CandidatePlanWitness,
    PandaPlanningBackend,
    PlanningBackendBridge,
    PlanningBackendResult,
    PlanningLimits,
    PlanningProblemSnapshot,
)
from agent_orchestrator.planning.htn.backends.panda import parse_plan
from agent_orchestrator.storage.htn_store import PlanCommitReceipt


def snapshot() -> PlanningProblemSnapshot:
    return PlanningProblemSnapshot.build("mission-1", 4, {"goals": ["g1"], "methods": ["m1"]})


def receipt() -> PlanCommitReceipt:
    return PlanCommitReceipt(
        command_id="solver-command",
        mission_id="mission-1",
        delta_id="solver-delta",
        base_plan_revision=4,
        new_plan_revision=5,
        intent_hash="a" * 64,
        read_set_hash="b" * 64,
        read_set=SemanticReadSet(requirements_revision=0),
        output_identity={"plan_revision": 5},
        detail={"source": "test-commit-port"},
    )


class RecordingCommitPort:
    def __init__(self) -> None:
        self.calls = []
        self.receipt = receipt()

    def commit_backend_plan(self, frozen_snapshot, witness):
        self.calls.append((frozen_snapshot, witness))
        return self.receipt


@pytest.mark.parametrize(("external", "expected"), (
    ("UNSOLVABLE_PROVEN", BackendStatus.UNSOLVABLE_PROVEN),
    ("SEARCH_LIMIT_REACHED", BackendStatus.SEARCH_LIMIT_REACHED),
    ("TIMEOUT", BackendStatus.TIMEOUT),
    ("UNSUPPORTED_FEATURE", BackendStatus.UNSUPPORTED_FEATURE),
    ("SOLVER_UNAVAILABLE", BackendStatus.BACKEND_UNAVAILABLE),
    ("TOOL_ERROR", BackendStatus.TOOL_ERROR),
))
def test_panda_non_solved_statuses_keep_their_meaning(external, expected):
    current = PlanningProblemSnapshot.build("mission", 1, {"domain_text": "domain", "problem_text": "problem"})
    toolchain = SimpleNamespace(solve=lambda *a, **kw: SimpleNamespace(status=external, witness=None, detail=""))
    result = PandaPlanningBackend(toolchain).solve(current, PlanningLimits(1, None, 4096))
    assert result.status is expected
    assert result.witness is None


def test_snapshot_mutation_cannot_reach_the_commit_port():
    current = snapshot()
    result = PlanningBackendResult(BackendStatus.SOLVED, current.digest,
        witness=CandidatePlanWitness.build(current, ("move(a)",), "panda"))
    current.problem["goals"].append("unapproved-goal")
    port = RecordingCommitPort()
    with pytest.raises(ContractError, match="changed after freezing"):
        PlanningBackendBridge(port).commit(result, snapshot=current)
    assert port.calls == []


def test_tampered_witness_hash_cannot_validate():
    current = snapshot()
    witness = CandidatePlanWitness.build(current, ("move(a)",), "panda")
    result = PlanningBackendResult(BackendStatus.SOLVED, current.digest,
                                   witness=replace(witness, steps=("unapproved(a)",)))
    assert PlanningBackendBridge().validate(result) is False


def test_non_solved_with_valid_witness_cannot_validate():
    current = snapshot()
    witness = CandidatePlanWitness.build(current, ("move(a)",), "panda")
    result = PlanningBackendResult(BackendStatus.TIMEOUT, current.digest, witness=witness)
    assert PlanningBackendBridge().validate(result) is False


def test_panda_rejects_unsupported_expansion_limit_before_tool_call():
    calls = []
    def solve(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status="TIMEOUT", witness=None, detail="")
    current = PlanningProblemSnapshot.build("mission", 1, {"domain_text": "domain", "problem_text": "problem"})
    result = PandaPlanningBackend(SimpleNamespace(solve=solve)).solve(current, PlanningLimits(1, 100, 4096))
    assert result.status is BackendStatus.UNSUPPORTED_FEATURE
    assert calls == []


def test_problem_snapshot_and_limits_are_content_addressed() -> None:
    value = snapshot()
    assert (
        value.digest
        == PlanningProblemSnapshot.build(
            "mission-1", 4, {"methods": ["m1"], "goals": ["g1"]}
        ).digest
    )
    assert (
        PlanningLimits(timeout_seconds=1.0, max_expansions=10, max_tokens=100).to_json()[
            "max_tokens"
        ]
        == 100
    )
    with pytest.raises(ContractError):
        PlanningLimits(timeout_seconds=0, max_expansions=1, max_tokens=1)


def test_statuses_are_distinct_and_non_solved_cannot_commit() -> None:
    bridge = PlanningBackendBridge()
    for status in (
        BackendStatus.UNSOLVABLE_PROVEN,
        BackendStatus.SEARCH_LIMIT_REACHED,
        BackendStatus.TIMEOUT,
        BackendStatus.UNSUPPORTED_FEATURE,
        BackendStatus.BACKEND_UNAVAILABLE,
        BackendStatus.TOOL_ERROR,
    ):
        result = PlanningBackendResult(
            status=status, snapshot_digest=snapshot().digest, detail=status.value
        )
        assert bridge.validate(result) is False
        with pytest.raises(ContractError, match="cannot commit"):
            bridge.commit(result, snapshot=snapshot())


def test_solved_witness_must_match_snapshot_and_is_validated_before_commit() -> None:
    current = snapshot()
    witness = CandidatePlanWitness.build(current, ("move(a)", "inspect(a)"), "panda-v1")
    result = PlanningBackendResult(BackendStatus.SOLVED, current.digest, witness=witness)
    port = RecordingCommitPort()
    bridge = PlanningBackendBridge(port)
    assert bridge.validate(result)
    assert bridge.commit(result, snapshot=current) is port.receipt
    assert port.calls == [(current, witness)]
    with pytest.raises(ContractError, match="deployed SH compiler"):
        PlanningBackendBridge().commit(result, snapshot=current)
    stale = PlanningBackendResult(BackendStatus.SOLVED, "0" * 64, witness=witness)
    assert bridge.validate(stale) is False


def test_malformed_solved_result_fails_closed() -> None:
    current = snapshot()
    result = PlanningBackendResult(BackendStatus.SOLVED, current.digest, witness=None)
    with pytest.raises(ContractError, match="witness"):
        PlanningBackendBridge(RecordingCommitPort()).commit(result, snapshot=current)


def test_panda_adapter_maps_a_solved_witness_into_the_port() -> None:
    class FakePanda:
        def solve(self, domain_text, problem_text, *, timeout_s, search_limit):
            del domain_text, problem_text, timeout_s, search_limit
            return type(
                "Solve",
                (),
                {
                    "status": "SOLVED",
                    "witness": parse_plan("==>\n0 move a\nroot 0"),
                    "detail": "",
                },
            )()

    current = PlanningProblemSnapshot.build(
        "mission-1",
        4,
        {
            "domain_text": "(define (domain d))",
            "problem_text": "(define (problem p))",
        },
    )
    limits = PlanningLimits(timeout_seconds=2.0, max_expansions=None, max_tokens=4096)
    result = PandaPlanningBackend(FakePanda()).solve(current, limits)
    assert result.status is BackendStatus.SOLVED
    assert result.witness is not None
    port = RecordingCommitPort()
    committed = PlanningBackendBridge(port).solve_and_commit(
        PandaPlanningBackend(FakePanda()), current, limits
    )
    assert committed is port.receipt
    assert port.calls[0][0] == current
    assert port.calls[0][1].snapshot_digest == current.digest
