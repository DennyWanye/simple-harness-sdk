"""Backend-neutral planning port and SH validation/commit bridge (H7)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ...storage.htn_store import PlanCommitReceipt

from simple_harness.contracts import canonical_json

from ...contracts.models import ContractError
from ...contracts.semantic_base import content_hash_of


class BackendStatus(StrEnum):
    SOLVED = "SOLVED"
    UNSOLVABLE_PROVEN = "UNSOLVABLE_PROVEN"
    SEARCH_LIMIT_REACHED = "SEARCH_LIMIT_REACHED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"


@dataclass(frozen=True, slots=True)
class PlanningProblemSnapshot:
    mission_id: str
    plan_revision: int
    problem: Mapping[str, Any]
    digest: str

    @classmethod
    def build(
        cls, mission_id: str, plan_revision: int, problem: Mapping[str, Any]
    ) -> PlanningProblemSnapshot:
        if not mission_id.strip() or plan_revision < 0:
            raise ContractError("invalid planning snapshot identity")
        normalized = json.loads(canonical_json(dict(problem)))
        return cls(
            mission_id,
            plan_revision,
            normalized,
            content_hash_of(
                {"mission_id": mission_id, "plan_revision": plan_revision, "problem": normalized}
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanningLimits:
    timeout_seconds: float
    max_expansions: int | None
    max_tokens: int

    def __post_init__(self) -> None:
        import math
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0 or type(self.max_tokens) is not int or self.max_tokens < 1
            or (self.max_expansions is not None and (type(self.max_expansions) is not int or self.max_expansions < 1))):
            raise ContractError("planning limits must be finite positive numbers, expansions may be null")

    def to_json(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_expansions": self.max_expansions,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True, slots=True)
class CandidatePlanWitness:
    backend_id: str
    snapshot_digest: str
    steps: tuple[str, ...]
    plan_hash: str
    hierarchy_json: str = ""

    @classmethod
    def build(
        cls,
        snapshot: PlanningProblemSnapshot,
        steps: Sequence[str],
        backend_id: str,
        *,
        hierarchy: Mapping[str, Any] | None = None,
    ) -> CandidatePlanWitness:
        normalized = tuple(str(item) for item in steps)
        if not backend_id.strip() or (not normalized and not hierarchy):
            raise ContractError("candidate witness requires backend and steps")
        hierarchy_json = "" if hierarchy is None else canonical_json(dict(hierarchy))
        body = {"snapshot_digest": snapshot.digest, "steps": list(normalized)}
        if hierarchy_json:
            body["hierarchy_json"] = hierarchy_json
        return cls(backend_id, snapshot.digest, normalized, content_hash_of(body), hierarchy_json)

    def valid_hash(self) -> bool:
        body: dict[str, Any] = {"snapshot_digest": self.snapshot_digest, "steps": list(self.steps)}
        if self.hierarchy_json:
            body["hierarchy_json"] = self.hierarchy_json
        return bool(self.backend_id) and self.plan_hash == content_hash_of(body)


@dataclass(frozen=True, slots=True)
class PlanningBackendResult:
    status: BackendStatus
    snapshot_digest: str
    witness: CandidatePlanWitness | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, BackendStatus):
            object.__setattr__(self, "status", BackendStatus(self.status))


class PlanningBackend(Protocol):
    backend_id: str

    def solve(
        self, snapshot: PlanningProblemSnapshot, limits: PlanningLimits
    ) -> PlanningBackendResult: ...


class PandaPlanningBackend:
    """Adapter from the existing PANDA toolchain to the H7 port.

    The snapshot carries the already compiled ``domain_text`` and
    ``problem_text``.  This keeps export/compilation outside the backend and
    makes the boundary explicit: a solver result is still only a witness until
    :class:`PlanningBackendBridge` validates it.
    """

    backend_id = "panda"

    def __init__(self, toolchain: Any) -> None:
        self.toolchain = toolchain

    def solve(
        self, snapshot: PlanningProblemSnapshot, limits: PlanningLimits
    ) -> PlanningBackendResult:
        if limits.max_expansions is not None:
            return PlanningBackendResult(BackendStatus.UNSUPPORTED_FEATURE, snapshot.digest,
                detail="PANDA wrapper exposes a time bound, not a hard expansion bound")
        domain_text = snapshot.problem.get("domain_text")
        problem_text = snapshot.problem.get("problem_text")
        if not isinstance(domain_text, str) or not isinstance(problem_text, str):
            return PlanningBackendResult(
                BackendStatus.TOOL_ERROR,
                snapshot.digest,
                detail="PANDA adapter requires compiled domain_text and problem_text",
            )
        try:
            result = self.toolchain.solve(
                domain_text,
                problem_text,
                timeout_s=limits.timeout_seconds,
                search_limit=max(1, int(limits.timeout_seconds)),
            )
        except Exception as error:  # pragma: no cover - defensive adapter boundary
            return PlanningBackendResult(
                BackendStatus.TOOL_ERROR,
                snapshot.digest,
                detail=f"PANDA adapter raised {type(error).__name__}: {error}",
            )
        status_map = {
            "SOLVED": BackendStatus.SOLVED,
            "UNSOLVABLE_PROVEN": BackendStatus.UNSOLVABLE_PROVEN,
            "SEARCH_LIMIT_REACHED": BackendStatus.SEARCH_LIMIT_REACHED,
            "TIMEOUT": BackendStatus.TIMEOUT,
            "UNSUPPORTED_FEATURE": BackendStatus.UNSUPPORTED_FEATURE,
            "SOLVER_UNAVAILABLE": BackendStatus.BACKEND_UNAVAILABLE,
            "TOOL_ERROR": BackendStatus.TOOL_ERROR,
        }
        status = status_map.get(str(result.status), BackendStatus.TOOL_ERROR)
        witness = None
        if status is BackendStatus.SOLVED and result.witness is not None:
            steps = tuple(item.signature for item in result.witness.primitives)
            from dataclasses import asdict
            import json

            witness = CandidatePlanWitness.build(
                snapshot, steps, "panda", hierarchy=json.loads(json.dumps(asdict(result.witness)))
            )
        if witness is not None and len(canonical_json({"steps": list(witness.steps), "hierarchy": witness.hierarchy_json}).encode("utf-8")) > limits.max_tokens:
            return PlanningBackendResult(BackendStatus.SEARCH_LIMIT_REACHED, snapshot.digest,
                detail="PANDA witness exceeds the conservative byte budget")
        if status is BackendStatus.SOLVED and witness is None:
            return PlanningBackendResult(BackendStatus.TOOL_ERROR, snapshot.digest,
                detail="solver claimed SOLVED without a witness")
        return PlanningBackendResult(status, snapshot.digest, witness=witness, detail=result.detail)


class BackendPlanCommitPort(Protocol):
    """Host-side compiler and original guarded CommitService, never a status stub."""

    def commit_backend_plan(
        self,
        snapshot: PlanningProblemSnapshot,
        witness: CandidatePlanWitness,
    ) -> PlanCommitReceipt: ...


class PlanningBackendBridge:
    def __init__(self, commit_port: BackendPlanCommitPort | None = None) -> None:
        self.commit_port = commit_port

    def validate(self, result: PlanningBackendResult) -> bool:
        return (
            result.status is BackendStatus.SOLVED
            and result.witness is not None
            and result.witness.snapshot_digest == result.snapshot_digest
            and result.witness.valid_hash()
        )

    def commit(
        self, result: PlanningBackendResult, *, snapshot: PlanningProblemSnapshot | None = None
    ) -> PlanCommitReceipt:
        if not self.validate(result):
            raise ContractError(
                f"backend status {result.status} cannot commit without a valid witness"
            )
        if snapshot is None or snapshot.digest != result.snapshot_digest:
            raise ContractError("backend commit requires its exact frozen problem snapshot")
        rebuilt = PlanningProblemSnapshot.build(
            snapshot.mission_id, snapshot.plan_revision, snapshot.problem
        )
        if rebuilt.digest != snapshot.digest:
            raise ContractError("backend problem snapshot changed after freezing")
        if self.commit_port is None:
            raise ContractError("backend commit requires a deployed SH compiler/CommitService port")
        assert result.witness is not None
        return self.commit_port.commit_backend_plan(snapshot, result.witness)

    def solve_and_commit(
        self, backend: PlanningBackend, snapshot: PlanningProblemSnapshot, limits: PlanningLimits
    ) -> PlanCommitReceipt:
        result = backend.solve(snapshot, limits)
        return self.commit(result, snapshot=snapshot)


__all__ = (
    "BackendStatus",
    "BackendPlanCommitPort",
    "CandidatePlanWitness",
    "PlanningBackend",
    "PandaPlanningBackend",
    "PlanningBackendBridge",
    "PlanningBackendResult",
    "PlanningLimits",
    "PlanningProblemSnapshot",
)
