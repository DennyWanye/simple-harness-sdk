# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The six core data contracts of the design's §26, frozen before anything else.

Mission (§26.1), Task Contract (§26.2), Attempt (§26.3), Result Envelope (§26.4),
Claim / Knowledge (§26.5), Event (§26.6).  Every object round-trips through
``to_json`` / ``from_json`` with canonical JSON so hashes are stable.  Field names
follow §26 verbatim; §13's ``result_id`` spelling is only accepted by the explicit
compatibility adapter in the collector (ORCH-BUILD §13).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from simple_harness.contracts import canonical_json

from .state_machines import (
    AttemptStatus,
    ClaimStatus,
    MissionStatus,
    ResultOutcome,
    TaskStatus,
)

CONTRACT_SCHEMA_VERSION = 2
MAX_TEXT = 20_000
MAX_LIST = 256

VERIFICATION_LAYERS = (
    "format_check",
    "rule_check",
    "critic_review",
    "code_test",
    "formal_check",
    "human_review",
)
STEP2_IMPLEMENTED_LAYERS = frozenset(
    {"format_check", "rule_check", "critic_review", "code_test", "human_review"}  # + step 7 (D7-8)
)
# the system default for a Task whose proposal names no verification policy (a Manager
# ``add_task``, a synthesis template)
SYSTEM_DEFAULT_POLICY = ("format_check", "rule_check", "code_test")


def default_change_policy(
    deployed: frozenset[str] = STEP2_IMPLEMENTED_LAYERS,
) -> tuple[str, ...]:
    """Host support 0.9.8: the system default narrowed to what the deployment runs.  Without
    local code execution the substantive check is the independent Critic instead of the
    tests — never a policy with no check beyond format and rules."""

    if "code_test" in deployed:
        return SYSTEM_DEFAULT_POLICY
    return ("format_check", "rule_check", "critic_review")


TASK_KINDS = ("work", "conflict", "synthesis")


class ContractError(ValueError):
    """A contract object is malformed (never a model-behaviour error)."""


def _text(value: object, name: str, *, allow_blank: bool = False, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    if not allow_blank and not value.strip():
        raise ContractError(f"{name} must not be blank")
    if len(value) > limit:
        raise ContractError(f"{name} exceeds {limit} characters")
    return value


def _texts(value: object, name: str, *, limit: int = MAX_LIST) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContractError(f"{name} must be a list of strings")
    items = tuple(_text(item, f"{name}[]") for item in value)
    if len(items) > limit:
        raise ContractError(f"{name} has more than {limit} entries")
    if len(set(items)) != len(items):
        raise ContractError(f"{name} must not contain duplicates")
    return items


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a non-negative integer or null")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ContractError(f"{name} must be finite")
    return float(value)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise ContractError(f"{name} keys must be strings")
    canonical_json(dict(value))  # must be JSON-representable
    return dict(value)


def _objects(value: object, name: str) -> tuple[dict[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{name} must be a list of objects")
    return tuple(_object(item, f"{name}[]") for item in value)


def _enum(kind, value: object, name: str):  # type: ignore[no-untyped-def]
    try:
        return kind(value)
    except ValueError as error:
        raise ContractError(f"{name} is not one of {[str(item) for item in kind]}") from error


def jsonable(value: object) -> Any:
    """Coerce an SDK-side structure (tuples, sets, enums, dataclasses) into plain JSON
    values so it can enter a contract object; strings are kept, unknown scalars become
    their ``str`` (step 4 real-run finding: a turn error carried a tuple)."""

    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if isinstance(value, (set, frozenset)):
            items = sorted(items, key=str)
        return [jsonable(item) for item in items]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return jsonable(to_json())
    return str(value)


def sha256_hex(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Budget:
    """§18.1: a budget is more than money.  ``None`` means "not limited at this level"."""

    max_tokens: int | None = None
    max_cost_micros: int | None = None
    max_attempts: int | None = None
    max_runtime_seconds: int | None = None
    max_concurrency: int | None = None
    max_tool_calls: int | None = None  # step 6 (D6-8): §18.1 "工具调用次数"

    def __post_init__(self) -> None:
        for name in fields(self):
            object.__setattr__(self, name.name, _optional_int(getattr(self, name.name), name.name))

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, value: object) -> Budget:
        data = _object(value, "budget")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ContractError(f"budget has unknown fields: {sorted(unknown)}")
        return cls(**data)

    def fits_within(self, parent: Budget) -> bool:
        """§18.2: a child budget never exceeds its parent on any limited dimension."""

        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(parent, f.name)
            if theirs is not None and (mine is None or mine > theirs):
                return False
        return True


@dataclass(frozen=True, slots=True)
class Mission:
    """§26.1 Mission Schema (+ ORCH-BUILD §13 status set and ``stop_reason``)."""

    id: str
    goal: str
    success_criteria: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    risk_level: str
    budget: Budget
    tenant_id: str
    status: MissionStatus
    created_at: float
    version: int
    idempotency_key: str
    stop_reason: str | None = None
    final_report: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "mission.id", limit=256))
        object.__setattr__(self, "goal", _text(self.goal, "mission.goal"))
        object.__setattr__(
            self, "success_criteria", _texts(self.success_criteria, "mission.success_criteria")
        )
        if not self.success_criteria:
            raise ContractError("mission.success_criteria must not be empty")
        object.__setattr__(
            self, "stop_conditions", _texts(self.stop_conditions, "mission.stop_conditions")
        )
        object.__setattr__(
            self, "allowed_tools", _texts(self.allowed_tools, "mission.allowed_tools")
        )
        object.__setattr__(
            self, "risk_level", _text(self.risk_level, "mission.risk_level", limit=64)
        )
        if not isinstance(self.budget, Budget):
            raise ContractError("mission.budget must be a Budget")
        object.__setattr__(self, "tenant_id", _text(self.tenant_id, "mission.tenant_id", limit=256))
        object.__setattr__(self, "status", _enum(MissionStatus, self.status, "mission.status"))
        _number(self.created_at, "mission.created_at")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ContractError("mission.version must be a positive integer")
        object.__setattr__(
            self,
            "idempotency_key",
            _text(self.idempotency_key, "mission.idempotency_key", limit=512),
        )
        if self.stop_reason is not None:
            object.__setattr__(
                self, "stop_reason", _text(self.stop_reason, "mission.stop_reason", limit=128)
            )
        if self.final_report is not None:
            object.__setattr__(
                self, "final_report", _object(self.final_report, "mission.final_report")
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "stop_conditions": list(self.stop_conditions),
            "allowed_tools": list(self.allowed_tools),
            "risk_level": self.risk_level,
            "budget": self.budget.to_json(),
            "tenant_id": self.tenant_id,
            "status": str(self.status),
            "created_at": self.created_at,
            "version": self.version,
            "idempotency_key": self.idempotency_key,
            "stop_reason": self.stop_reason,
            "final_report": None if self.final_report is None else dict(self.final_report),
        }

    @classmethod
    def from_json(cls, value: object) -> Mission:
        data = _object(value, "mission")
        return cls(
            id=data["id"],
            goal=data["goal"],
            success_criteria=tuple(data.get("success_criteria", ())),
            stop_conditions=tuple(data.get("stop_conditions", ())),
            allowed_tools=tuple(data.get("allowed_tools", ())),
            risk_level=data["risk_level"],
            budget=Budget.from_json(data.get("budget", {})),
            tenant_id=data["tenant_id"],
            status=data["status"],
            created_at=data["created_at"],
            version=data["version"],
            idempotency_key=data["idempotency_key"],
            stop_reason=data.get("stop_reason"),
            final_report=data.get("final_report"),
        )


@dataclass(frozen=True, slots=True)
class Task:
    """§26.2 Task Contract (§6.3 adds ``root_goal``)."""

    id: str
    mission_id: str
    parent_task_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    goal: str
    rationale: str
    success_criteria: tuple[str, ...]
    verification_policy: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budget: Budget
    priority: float
    status: TaskStatus
    version: int
    root_goal: str = ""
    created_at: float = 0.0
    accepted_result_id: str | None = None
    accepted_artifacts: tuple[str, ...] = ()
    attempt_count: int = 0
    failure_reason: str | None = None
    outputs: tuple[str, ...] = ()  # step 3 (D3-7'): upstream paths this Task may rewrite
    kind: str = "work"  # step 4 (D4-7/D4-8): work | conflict | synthesis (system templates)
    context: Mapping[str, Any] = field(default_factory=dict)  # system data of a template Task
    ready_at: float | None = None  # step 5 (D5-8): when the Task became READY (waiting_age)
    paused: bool = False  # step 5 (D5-1): Manager pause — a scheduling flag, not a state
    pause_reason: str | None = None

    def __hash__(self) -> int:  # P2-15: ``context`` is a dict; identity is (id, version)
        return hash((self.id, self.version))

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "task.id", limit=256))
        object.__setattr__(self, "outputs", _texts(self.outputs, "task.outputs"))
        if self.kind not in TASK_KINDS:
            raise ContractError(f"task.kind must be one of {list(TASK_KINDS)}")
        object.__setattr__(self, "context", _object(self.context, "task.context"))
        object.__setattr__(self, "mission_id", _text(self.mission_id, "task.mission_id", limit=256))
        object.__setattr__(
            self, "parent_task_ids", _texts(self.parent_task_ids, "task.parent_task_ids")
        )
        object.__setattr__(
            self, "dependency_ids", _texts(self.dependency_ids, "task.dependency_ids")
        )
        object.__setattr__(self, "goal", _text(self.goal, "task.goal"))
        object.__setattr__(self, "rationale", _text(self.rationale, "task.rationale"))
        object.__setattr__(
            self, "success_criteria", _texts(self.success_criteria, "task.success_criteria")
        )
        if not self.success_criteria:
            raise ContractError("task.success_criteria must not be empty (§6.3: 完成条件不清)")
        policy = _texts(self.verification_policy, "task.verification_policy")
        unknown = [layer for layer in policy if layer not in VERIFICATION_LAYERS]
        if unknown:
            raise ContractError(f"task.verification_policy has unknown layers: {unknown}")
        if not policy:
            raise ContractError("task.verification_policy must name at least one layer")
        object.__setattr__(self, "verification_policy", policy)
        object.__setattr__(self, "allowed_tools", _texts(self.allowed_tools, "task.allowed_tools"))
        if not isinstance(self.budget, Budget):
            raise ContractError("task.budget must be a Budget")
        object.__setattr__(self, "priority", _number(self.priority, "task.priority"))
        object.__setattr__(self, "status", _enum(TaskStatus, self.status, "task.status"))
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ContractError("task.version must be a positive integer")
        object.__setattr__(
            self, "root_goal", _text(self.root_goal, "task.root_goal", allow_blank=True)
        )
        _number(self.created_at, "task.created_at")
        object.__setattr__(
            self, "accepted_artifacts", _texts(self.accepted_artifacts, "task.accepted_artifacts")
        )
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ContractError("task.attempt_count must be a non-negative integer")

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mission_id": self.mission_id,
            "parent_task_ids": list(self.parent_task_ids),
            "dependency_ids": list(self.dependency_ids),
            "goal": self.goal,
            "rationale": self.rationale,
            "success_criteria": list(self.success_criteria),
            "verification_policy": list(self.verification_policy),
            "allowed_tools": list(self.allowed_tools),
            "budget": self.budget.to_json(),
            "priority": self.priority,
            "status": str(self.status),
            "version": self.version,
            "root_goal": self.root_goal,
            "created_at": self.created_at,
            "accepted_result_id": self.accepted_result_id,
            "accepted_artifacts": list(self.accepted_artifacts),
            "attempt_count": self.attempt_count,
            "failure_reason": self.failure_reason,
            "outputs": list(self.outputs),
            "kind": self.kind,
            "context": dict(self.context),
            "ready_at": self.ready_at,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
        }

    @classmethod
    def from_json(cls, value: object) -> Task:
        data = _object(value, "task")
        return cls(
            id=data["id"],
            mission_id=data["mission_id"],
            parent_task_ids=tuple(data.get("parent_task_ids", ())),
            dependency_ids=tuple(data.get("dependency_ids", ())),
            goal=data["goal"],
            rationale=data["rationale"],
            success_criteria=tuple(data.get("success_criteria", ())),
            verification_policy=tuple(data.get("verification_policy", ())),
            allowed_tools=tuple(data.get("allowed_tools", ())),
            budget=Budget.from_json(data.get("budget", {})),
            priority=data.get("priority", 0.0),
            status=data["status"],
            version=data["version"],
            root_goal=data.get("root_goal", ""),
            created_at=data.get("created_at", 0.0),
            accepted_result_id=data.get("accepted_result_id"),
            accepted_artifacts=tuple(data.get("accepted_artifacts", ())),
            attempt_count=data.get("attempt_count", 0),
            failure_reason=data.get("failure_reason"),
            outputs=tuple(data.get("outputs", ())),
            kind=data.get("kind", "work") or "work",
            context=data.get("context", {}) or {},
            ready_at=data.get("ready_at"),
            paused=bool(data.get("paused", False)),
            pause_reason=data.get("pause_reason"),
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    """§26.3 Attempt Schema plus the durable binding to the SDK execution identity.

    ``agent_id`` / ``turn_id`` are filled by the dispatcher from real SDK receipts;
    they are never guessed (ORCH-BUILD §4.3).
    """

    id: str
    task_id: str
    mission_id: str
    role: str
    model: str
    prompt_version: str
    context_version: str
    budget_reserved: Budget
    lease_owner: str | None
    lease_expires_at: float | None
    status: AttemptStatus
    retry_of: str | None
    created_at: float
    version: int
    ordinal: int
    creation_key: str
    input_id: str
    task_version: int = 1
    runtime_profile_id: str = "default"  # step 6 (D6-4): the physical pool this Attempt is bound to
    agent_id: str | None = None
    turn_id: str | None = None
    input_hash: str | None = None
    feedback: tuple[str, ...] = ()
    failure: Mapping[str, Any] | None = None
    result_id: str | None = None
    progress_marker: int | None = None
    progress_at: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "id",
            "task_id",
            "mission_id",
            "role",
            "model",
            "prompt_version",
            "context_version",
            "creation_key",
            "input_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), f"attempt.{name}", limit=512))
        if not isinstance(self.budget_reserved, Budget):
            raise ContractError("attempt.budget_reserved must be a Budget")
        object.__setattr__(self, "status", _enum(AttemptStatus, self.status, "attempt.status"))
        _number(self.created_at, "attempt.created_at")
        for name in ("version", "ordinal", "task_version"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractError(f"attempt.{name} must be a positive integer")
        if self.lease_expires_at is not None:
            _number(self.lease_expires_at, "attempt.lease_expires_at")
        object.__setattr__(
            self, "feedback", tuple(_text(item, "attempt.feedback[]") for item in self.feedback)
        )
        if self.failure is not None:
            object.__setattr__(self, "failure", _object(self.failure, "attempt.failure"))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "role": self.role,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "context_version": self.context_version,
            "budget_reserved": self.budget_reserved.to_json(),
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "status": str(self.status),
            "retry_of": self.retry_of,
            "created_at": self.created_at,
            "version": self.version,
            "ordinal": self.ordinal,
            "creation_key": self.creation_key,
            "input_id": self.input_id,
            "task_version": self.task_version,
            "runtime_profile_id": self.runtime_profile_id,
            "agent_id": self.agent_id,
            "turn_id": self.turn_id,
            "input_hash": self.input_hash,
            "feedback": list(self.feedback),
            "progress_marker": self.progress_marker,
            "progress_at": self.progress_at,
            "failure": None if self.failure is None else dict(self.failure),
            "result_id": self.result_id,
        }

    @classmethod
    def from_json(cls, value: object) -> Attempt:
        data = _object(value, "attempt")
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            mission_id=data["mission_id"],
            role=data["role"],
            model=data["model"],
            prompt_version=data["prompt_version"],
            context_version=data["context_version"],
            budget_reserved=Budget.from_json(data.get("budget_reserved", {})),
            lease_owner=data.get("lease_owner"),
            lease_expires_at=data.get("lease_expires_at"),
            status=data["status"],
            retry_of=data.get("retry_of"),
            created_at=data["created_at"],
            version=data["version"],
            ordinal=data["ordinal"],
            creation_key=data["creation_key"],
            input_id=data["input_id"],
            task_version=data.get("task_version", 1),
            runtime_profile_id=str(data.get("runtime_profile_id", "default")),
            agent_id=data.get("agent_id"),
            turn_id=data.get("turn_id"),
            input_hash=data.get("input_hash"),
            feedback=tuple(data.get("feedback", ())),
            progress_marker=data.get("progress_marker"),
            progress_at=data.get("progress_at"),
            failure=data.get("failure"),
            result_id=data.get("result_id"),
        )


CLAIM_STANCES = ("affirms", "refutes")


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """A proposed quotation, never a model-controlled verification result.

    Integer range validity belongs to the resolver, which knows the source length.
    """

    path: str
    version: str
    start_line: int
    end_line: int
    quote: str

    def __post_init__(self) -> None:
        for name in ("path", "version", "quote"):
            _text(getattr(self, name), f"citation.{name}", allow_blank=name == "quote")
        for name in ("start_line", "end_line"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractError(f"citation.{name} must be an integer")

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, value: object) -> SourceCitation:
        data = _object(value, "citation")
        required = {f.name for f in fields(cls)}
        if unknown := set(data) - required:
            raise ContractError(f"citation has unknown fields: {sorted(unknown)}")
        if missing := required - set(data):
            raise ContractError(f"citation has missing fields: {sorted(missing)}")
        return cls(**data)


def _citation_items(value: object) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError("claim.citations must be an array")
    if len(value) > MAX_LIST:
        raise ContractError(f"claim.citations exceeds {MAX_LIST} items")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ClaimProposal:
    """A claim as it appears inside a Result Envelope (§13 ``claims[]``).

    Step 4 (D4-1): a claim may carry its own ``evidence`` (default: the envelope's),
    a subject ``key`` with a ``stance`` (two claims on one key with different stances
    contradict each other), an explicit ``supersedes`` (knowledge id) and a
    ``contradicts`` list (§12.3 冲突报告).  All of it is *data*: the system grades
    the claim from the verification it actually ran (D4-2).
    """

    content: str
    confidence: float
    status: ClaimStatus = ClaimStatus.PROPOSED
    type: str = "statement"
    evidence: tuple[str, ...] = ()
    key: str | None = None
    stance: str = "affirms"
    supersedes: str | None = None
    contradicts: tuple[str, ...] = ()
    citations: tuple[SourceCitation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", _text(self.content, "claim.content"))
        confidence = _number(self.confidence, "claim.confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ContractError("claim.confidence must be within [0, 1]")
        object.__setattr__(self, "confidence", confidence)
        status = _enum(ClaimStatus, self.status, "claim.status")
        if status is not ClaimStatus.PROPOSED:
            # 原则三: an Agent cannot submit anything above PROPOSED (§2.2 / §15).
            raise ContractError("an Agent may only propose claims with status PROPOSED")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "type", _text(self.type, "claim.type", limit=64))
        object.__setattr__(self, "evidence", _texts(self.evidence, "claim.evidence"))
        if self.key is not None:
            object.__setattr__(self, "key", _text(self.key, "claim.key", limit=128))
        if self.stance not in CLAIM_STANCES:
            raise ContractError(f"claim.stance must be one of {list(CLAIM_STANCES)}")
        if self.supersedes is not None:
            object.__setattr__(
                self, "supersedes", _text(self.supersedes, "claim.supersedes", limit=512)
            )
        object.__setattr__(self, "contradicts", _texts(self.contradicts, "claim.contradicts"))
        citations = _citation_items(self.citations)
        if any(not isinstance(item, SourceCitation) for item in citations):
            raise ContractError("claim.citations must contain SourceCitation objects")
        object.__setattr__(self, "citations", citations)

    def to_json(self) -> dict[str, Any]:
        result = {
            "content": self.content,
            "confidence": self.confidence,
            "status": str(self.status),
            "type": self.type,
            "evidence": list(self.evidence),
            "key": self.key,
            "stance": self.stance,
            "supersedes": self.supersedes,
            "contradicts": list(self.contradicts),
        }
        # Preserve canonical v1 envelope bytes and hashes when no citations exist.
        if self.citations:
            result["citations"] = [item.to_json() for item in self.citations]
        return result

    @classmethod
    def from_json(cls, value: object) -> ClaimProposal:
        data = _object(value, "claim")
        unknown = set(data) - {
            "content",
            "confidence",
            "status",
            "type",
            "evidence",
            "key",
            "stance",
            "supersedes",
            "contradicts",
            "citations",
        }
        if unknown:
            raise ContractError(f"claim has unknown fields: {sorted(unknown)}")
        return cls(
            content=data.get("content"),  # type: ignore[arg-type]
            confidence=data.get("confidence"),  # type: ignore[arg-type]
            status=data.get("status", "PROPOSED"),
            type=data.get("type", "statement"),
            evidence=tuple(data.get("evidence", ()) or ()),
            key=data.get("key"),
            stance=data.get("stance", "affirms") or "affirms",
            supersedes=data.get("supersedes"),
            contradicts=tuple(data.get("contradicts", ()) or ()),
            citations=tuple(
                SourceCitation.from_json(item)
                for item in _citation_items(data.get("citations", ()))
            ),
        )


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    """§26.4 Result Envelope: the only thing an Agent may submit (§13)."""

    id: str
    task_id: str
    attempt_id: str
    outcome: ResultOutcome
    summary: str
    claims: tuple[ClaimProposal, ...]
    evidence: tuple[str, ...]
    artifacts: tuple[str, ...]
    proposed_tasks: tuple[Mapping[str, Any], ...]
    used_knowledge: tuple[str, ...]
    risks: tuple[str, ...]
    cost: Mapping[str, Any]
    mission_id: str = ""

    def __post_init__(self) -> None:
        for name in ("id", "task_id", "attempt_id"):
            object.__setattr__(self, name, _text(getattr(self, name), f"result.{name}", limit=512))
        object.__setattr__(self, "outcome", _enum(ResultOutcome, self.outcome, "result.outcome"))
        object.__setattr__(self, "summary", _text(self.summary, "result.summary"))
        claims = tuple(self.claims)
        for claim in claims:
            if not isinstance(claim, ClaimProposal):
                raise ContractError("result.claims must contain ClaimProposal objects")
        object.__setattr__(self, "claims", claims)
        for name in ("evidence", "artifacts", "used_knowledge", "risks"):
            object.__setattr__(self, name, _texts(getattr(self, name), f"result.{name}"))
        object.__setattr__(
            self, "proposed_tasks", _objects(self.proposed_tasks, "result.proposed_tasks")
        )
        object.__setattr__(self, "cost", _object(self.cost, "result.cost"))
        object.__setattr__(
            self,
            "mission_id",
            _text(self.mission_id, "result.mission_id", allow_blank=True, limit=256),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "outcome": str(self.outcome),
            "summary": self.summary,
            "claims": [claim.to_json() for claim in self.claims],
            "evidence": list(self.evidence),
            "artifacts": list(self.artifacts),
            "proposed_tasks": [dict(item) for item in self.proposed_tasks],
            "used_knowledge": list(self.used_knowledge),
            "risks": list(self.risks),
            "cost": dict(self.cost),
        }

    @property
    def result_hash(self) -> str:
        return sha256_hex(self.to_json())

    @classmethod
    def from_json(cls, value: object, *, strict: bool = True) -> ResultEnvelope:
        """Parse an Agent-submitted envelope.

        ``strict`` refuses unknown keys and the §13 ``result_id`` alias unless it
        is the *only* id spelling present (ORCH-BUILD §13: both keys present and
        different → reject, never silently pick one).
        """

        data = _object(value, "result")
        if "result_id" in data:
            if "id" in data and data["id"] != data["result_id"]:
                raise ContractError("result carries both id and result_id with different values")
            data = {**data, "id": data["result_id"]}
            data.pop("result_id")
        allowed = {
            "id",
            "mission_id",
            "task_id",
            "attempt_id",
            "outcome",
            "summary",
            "claims",
            "evidence",
            "artifacts",
            "proposed_tasks",
            "used_knowledge",
            "risks",
            "cost",
        }
        unknown = set(data) - allowed
        if strict and unknown:
            raise ContractError(f"result has unknown fields: {sorted(unknown)}")
        missing = {"id", "task_id", "attempt_id", "outcome", "summary"} - set(data)
        if missing:
            raise ContractError(f"result is missing required fields: {sorted(missing)}")
        raw_claims = data.get("claims", ())
        if isinstance(raw_claims, (str, bytes)) or not isinstance(raw_claims, Sequence):
            raise ContractError("result.claims must be a list")
        return cls(
            id=data["id"],
            mission_id=data.get("mission_id", ""),
            task_id=data["task_id"],
            attempt_id=data["attempt_id"],
            outcome=data["outcome"],
            summary=data["summary"],
            claims=tuple(ClaimProposal.from_json(item) for item in raw_claims),
            evidence=tuple(data.get("evidence", ())),
            artifacts=tuple(data.get("artifacts", ())),
            proposed_tasks=tuple(data.get("proposed_tasks", ())),
            used_knowledge=tuple(data.get("used_knowledge", ())),
            risks=tuple(data.get("risks", ())),
            cost=data.get("cost", {}),
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """§26.5 Claim / Knowledge Schema (formal record owned by the Commit Service).

    Step 4 adds the subject ``key`` / ``stance``, who proposed it, and the dispute /
    resolution / supersession markers (data fields; the §25.3 status machine is not
    extended — a DISPUTED claim stays DISPUTED and points at what resolved it).
    """

    id: str
    content: str
    type: str
    status: ClaimStatus
    source_task: str
    source_attempt: str
    evidence: tuple[str, ...]
    dependencies: tuple[str, ...]
    verifier_results: tuple[Mapping[str, Any], ...]
    confidence_metadata: Mapping[str, Any]
    supersedes: str | None
    mission_id: str
    result_id: str
    version: int = 1
    key: str | None = None
    stance: str = "affirms"
    proposed_by: str = ""
    contradicts: tuple[str, ...] = ()
    superseded_by: str | None = None
    disputed_by: tuple[str, ...] = ()
    resolved_by: str | None = None
    conflict_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "source_task", "source_attempt", "mission_id", "result_id"):
            object.__setattr__(self, name, _text(getattr(self, name), f"claim.{name}", limit=512))
        object.__setattr__(self, "content", _text(self.content, "claim.content"))
        object.__setattr__(self, "type", _text(self.type, "claim.type", limit=64))
        object.__setattr__(self, "status", _enum(ClaimStatus, self.status, "claim.status"))
        object.__setattr__(self, "evidence", _texts(self.evidence, "claim.evidence"))
        object.__setattr__(self, "dependencies", _texts(self.dependencies, "claim.dependencies"))
        object.__setattr__(
            self, "verifier_results", _objects(self.verifier_results, "claim.verifier_results")
        )
        object.__setattr__(
            self,
            "confidence_metadata",
            _object(self.confidence_metadata, "claim.confidence_metadata"),
        )
        if self.key is not None:
            object.__setattr__(self, "key", _text(self.key, "claim.key", limit=128))
        if self.stance not in CLAIM_STANCES:
            raise ContractError(f"claim.stance must be one of {list(CLAIM_STANCES)}")
        object.__setattr__(
            self, "proposed_by", _text(self.proposed_by, "claim.proposed_by", allow_blank=True)
        )
        object.__setattr__(self, "contradicts", _texts(self.contradicts, "claim.contradicts"))
        object.__setattr__(self, "disputed_by", _texts(self.disputed_by, "claim.disputed_by"))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "type": self.type,
            "status": str(self.status),
            "source_task": self.source_task,
            "source_attempt": self.source_attempt,
            "evidence": list(self.evidence),
            "dependencies": list(self.dependencies),
            "verifier_results": [dict(item) for item in self.verifier_results],
            "confidence_metadata": dict(self.confidence_metadata),
            "supersedes": self.supersedes,
            "mission_id": self.mission_id,
            "result_id": self.result_id,
            "version": self.version,
            "key": self.key,
            "stance": self.stance,
            "proposed_by": self.proposed_by,
            "contradicts": list(self.contradicts),
            "superseded_by": self.superseded_by,
            "disputed_by": list(self.disputed_by),
            "resolved_by": self.resolved_by,
            "conflict_id": self.conflict_id,
        }

    @classmethod
    def from_json(cls, value: object) -> Claim:
        data = _object(value, "claim")
        return cls(
            id=data["id"],
            content=data["content"],
            type=data.get("type", "statement"),
            status=data["status"],
            source_task=data["source_task"],
            source_attempt=data["source_attempt"],
            evidence=tuple(data.get("evidence", ())),
            dependencies=tuple(data.get("dependencies", ())),
            verifier_results=tuple(data.get("verifier_results", ())),
            confidence_metadata=data.get("confidence_metadata", {}),
            supersedes=data.get("supersedes"),
            mission_id=data["mission_id"],
            result_id=data["result_id"],
            version=data.get("version", 1),
            key=data.get("key"),
            stance=data.get("stance", "affirms") or "affirms",
            proposed_by=data.get("proposed_by", "") or "",
            contradicts=tuple(data.get("contradicts", ()) or ()),
            superseded_by=data.get("superseded_by"),
            disputed_by=tuple(data.get("disputed_by", ()) or ()),
            resolved_by=data.get("resolved_by"),
            conflict_id=data.get("conflict_id"),
        )


@dataclass(frozen=True, slots=True)
class Event:
    """§26.6 Event Schema: an immutable fact; ``seq`` is assigned by the store."""

    id: str
    type: str
    trace_id: str
    mission_id: str
    task_id: str | None
    attempt_id: str | None
    actor_type: str
    actor_id: str
    payload: Mapping[str, Any]
    idempotency_key: str
    created_at: float
    schema_version: int = CONTRACT_SCHEMA_VERSION
    seq: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "id",
            "type",
            "trace_id",
            "mission_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), f"event.{name}", limit=512))
        object.__setattr__(self, "payload", _object(self.payload, "event.payload"))
        _number(self.created_at, "event.created_at")

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "trace_id": self.trace_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "seq": self.seq,
        }

    @classmethod
    def from_json(cls, value: object) -> Event:
        data = _object(value, "event")
        return cls(
            id=data["id"],
            type=data["type"],
            trace_id=data["trace_id"],
            mission_id=data["mission_id"],
            task_id=data.get("task_id"),
            attempt_id=data.get("attempt_id"),
            actor_type=data["actor_type"],
            actor_id=data["actor_id"],
            payload=data.get("payload", {}),
            idempotency_key=data["idempotency_key"],
            created_at=data["created_at"],
            schema_version=data.get("schema_version", CONTRACT_SCHEMA_VERSION),
            seq=data.get("seq"),
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    """§20.2 Artifact record (content-addressed file inside an Attempt workspace)."""

    id: str
    mission_id: str
    task_id: str
    attempt_id: str
    type: str
    path: str
    version: int
    content_hash: str
    size_bytes: int
    produced_by: str
    verification_status: str = "UNVERIFIED"
    storage_uri: str = ""
    created_at: float = 0.0
    workspace: str = ""  # step 6 (§20.2 / D6-6): the Attempt workspace that produced it

    def __post_init__(self) -> None:
        for name in ("id", "mission_id", "task_id", "attempt_id", "type", "path", "produced_by"):
            object.__setattr__(
                self, name, _text(getattr(self, name), f"artifact.{name}", limit=1024)
            )
        if len(self.content_hash) != 64:
            raise ContractError("artifact.content_hash must be a sha256 hex digest")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ContractError("artifact.size_bytes must be a non-negative integer")

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, value: object) -> Artifact:
        return cls(**_object(value, "artifact"))


__all__ = (
    "CLAIM_STANCES",
    "CONTRACT_SCHEMA_VERSION",
    "STEP2_IMPLEMENTED_LAYERS",
    "SYSTEM_DEFAULT_POLICY",
    "default_change_policy",
    "TASK_KINDS",
    "VERIFICATION_LAYERS",
    "Artifact",
    "Attempt",
    "Budget",
    "Claim",
    "ClaimProposal",
    "ContractError",
    "Event",
    "Mission",
    "ResultEnvelope",
    "SourceCitation",
    "Task",
    "jsonable",
    "sha256_hex",
)
