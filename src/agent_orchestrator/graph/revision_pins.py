"""Pure reconstruction and verification of TaskGraph revision pin rows."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from .network_codec import NetworkDocumentV1, decode


def _bad(message: str) -> NoReturn:
    raise ContractError(f"REVISION_PINS_INVALID: {message}")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _bad(f"{label} must be a nonempty identifier of at most 512 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _bad(f"{label} must be UTF-8")
    return value


def _revision(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        _bad(f"{label} must be a nonnegative safe integer")
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _bad(f"{label} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class MemberPin:
    mission_id: str
    revision: int
    occurrence_id: str
    task_id: str
    binding_revision: int
    binding_hash: str

    def __post_init__(self) -> None:
        _identifier(self.mission_id, "mission_id")
        _revision(self.revision, "revision")
        _identifier(self.occurrence_id, "occurrence_id")
        _identifier(self.task_id, "task_id")
        _revision(self.binding_revision, "binding_revision")
        _digest(self.binding_hash, "binding_hash")


@dataclass(frozen=True, slots=True, kw_only=True)
class MethodPin:
    mission_id: str
    revision: int
    instance_id: str
    goal_occurrence_id: str
    adopted: int
    draft_hash: str

    def __post_init__(self) -> None:
        _identifier(self.mission_id, "mission_id")
        _revision(self.revision, "revision")
        _identifier(self.instance_id, "instance_id")
        _identifier(self.goal_occurrence_id, "goal_occurrence_id")
        if type(self.adopted) is not int or self.adopted not in (0, 1):
            _bad("adopted must be integer 0 or 1")
        _digest(self.draft_hash, "draft_hash")


@dataclass(frozen=True, slots=True, kw_only=True)
class DemandRef:
    mission_id: str
    revision: int
    consumer_instance_id: str
    slot_key: str
    slot_occurrence_id: str
    producer_occurrence_id: str
    obligation_id: str
    mode: str
    requiredness: str
    source_slot_hash: str

    def __post_init__(self) -> None:
        _identifier(self.mission_id, "mission_id")
        _revision(self.revision, "revision")
        for label in (
            "consumer_instance_id",
            "slot_key",
            "slot_occurrence_id",
            "producer_occurrence_id",
            "obligation_id",
            "mode",
            "requiredness",
        ):
            _identifier(getattr(self, label), label)
        _digest(self.source_slot_hash, "source_slot_hash")
        if self.mode not in {"new_work", "reuse_accepted", "share_active"}:
            _bad("mode is not a supported reuse policy")
        if self.requiredness not in {"required", "optional_authorized", "conditional"}:
            _bad("requiredness is not a supported requirement mode")


@dataclass(frozen=True, slots=True, kw_only=True)
class RevisionPins:
    member_pins: tuple[MemberPin, ...]
    method_pins: tuple[MethodPin, ...]
    demand_refs: tuple[DemandRef, ...]

    def __post_init__(self) -> None:
        members = _sorted_unique(
            self.member_pins, key=lambda row: row.occurrence_id, label="member", row_type=MemberPin
        )
        methods = _sorted_unique(
            self.method_pins, key=lambda row: row.instance_id, label="method", row_type=MethodPin
        )
        demands = _sorted_unique(
            self.demand_refs,
            key=lambda row: (row.consumer_instance_id, row.slot_key),
            label="demand",
            row_type=DemandRef,
        )
        producer_obligations: dict[str, str] = {}
        for row in demands:
            previous = producer_obligations.setdefault(
                row.producer_occurrence_id, row.obligation_id
            )
            if previous != row.obligation_id:
                _bad("one producer occurrence cannot have multiple obligations")
        object.__setattr__(self, "member_pins", members)
        object.__setattr__(self, "method_pins", methods)
        object.__setattr__(self, "demand_refs", demands)


T = TypeVar("T")


def _sorted_unique(
    rows: Sequence[T], *, key: Callable[[T], Any], label: str, row_type: type[T]
) -> tuple[T, ...]:
    if not isinstance(rows, (tuple, list)) or not all(isinstance(row, row_type) for row in rows):
        _bad(f"{label} rows must be a sequence of {row_type.__name__}")
    ordered = tuple(sorted(rows, key=key))
    keys = tuple(key(row) for row in ordered)
    if len(set(keys)) != len(keys):
        _bad(f"{label} contains duplicate identity")
    return ordered


def build_revision_pins(document: NetworkDocumentV1) -> RevisionPins:
    decoded = decode(document.to_json()).snapshot
    mission, revision = str(decoded.mission_id), int(decoded.plan_revision)
    occurrences = {str(item.occurrence_id): item for item in decoded.occurrences}
    bindings = {str(item.task_id): item for item in decoded.task_bindings}
    members: list[MemberPin] = []
    for occurrence in decoded.occurrences:
        binding = bindings.get(str(occurrence.task_id))
        if binding is None:
            _bad("occurrence lacks task binding")
        members.append(
            MemberPin(
                mission_id=mission,
                revision=revision,
                occurrence_id=str(occurrence.occurrence_id),
                task_id=str(occurrence.task_id),
                binding_revision=int(binding.contract_revision),
                binding_hash=binding.content_hash(),
            )
        )

    methods: list[MethodPin] = []
    demands: list[DemandRef] = []
    adopted = {str(item) for item in document.adopted_instance_ids}
    for draft in decoded.method_instances:
        instance_id = str(draft.instance_id)
        adopted_flag = int(instance_id in adopted)
        methods.append(
            MethodPin(
                mission_id=mission,
                revision=revision,
                instance_id=instance_id,
                goal_occurrence_id=str(draft.effective_goal_occurrence_id),
                adopted=adopted_flag,
                draft_hash=hashlib.sha256(
                    canonical_json(draft.to_json()).encode("utf-8")
                ).hexdigest(),
            )
        )
        if not adopted_flag:
            continue
        for slot in draft.child_bindings:
            slot_id = str(slot.occurrence_id)
            producer_id = str(slot.goal_occurrence_id or slot.occurrence_id)
            if slot_id not in occurrences or producer_id not in occurrences:
                _bad("child binding references absent occurrence")
            expected_obligation = str(occurrences[producer_id].obligation_id)
            if str(slot.obligation_id) != expected_obligation:
                _bad("child binding obligation disagrees with effective producer")
            demands.append(
                DemandRef(
                    mission_id=mission,
                    revision=revision,
                    consumer_instance_id=instance_id,
                    slot_key=slot.slot_key,
                    slot_occurrence_id=slot_id,
                    producer_occurrence_id=producer_id,
                    obligation_id=expected_obligation,
                    mode=str(slot.reuse_policy),
                    requiredness=str(slot.requiredness),
                    source_slot_hash=hashlib.sha256(
                        canonical_json(slot.to_json()).encode("utf-8")
                    ).hexdigest(),
                )
            )
    return RevisionPins(
        member_pins=_sorted_unique(
            members, key=lambda row: row.occurrence_id, label="member", row_type=MemberPin
        ),
        method_pins=_sorted_unique(
            methods, key=lambda row: row.instance_id, label="method", row_type=MethodPin
        ),
        demand_refs=_sorted_unique(
            demands,
            key=lambda row: (row.consumer_instance_id, row.slot_key),
            label="demand",
            row_type=DemandRef,
        ),
    )


def verify_revision_pins(
    document: NetworkDocumentV1,
    persisted_member_rows: tuple[MemberPin, ...],
    persisted_method_rows: tuple[MethodPin, ...],
    persisted_demand_rows: tuple[DemandRef, ...],
) -> None:
    expected = build_revision_pins(document)
    actual_rows = (
        (
            "member",
            _sorted_unique(
                persisted_member_rows,
                key=lambda row: row.occurrence_id,
                label="member",
                row_type=MemberPin,
            ),
            expected.member_pins,
        ),
        (
            "method",
            _sorted_unique(
                persisted_method_rows,
                key=lambda row: row.instance_id,
                label="method",
                row_type=MethodPin,
            ),
            expected.method_pins,
        ),
        (
            "demand",
            _sorted_unique(
                persisted_demand_rows,
                key=lambda row: (row.consumer_instance_id, row.slot_key),
                label="demand",
                row_type=DemandRef,
            ),
            expected.demand_refs,
        ),
    )
    for label, actual, wanted in actual_rows:
        if tuple(actual) != tuple(wanted):
            _bad(f"{label} pins differ: missing, extra, duplicate, or value mismatch")
