# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The trusted predicate registry (§6.6, §7.3 step 1, AER §8.3).

A predicate is *declared*, never supplied as code.  The registry stores a typed
signature, which observers can produce it, and whether its domain is open or
closed — and nothing that could be executed: :func:`PredicateRegistry.register`
refuses callables, SQL fragments and ``eval``-shaped strings anywhere in the
declaration.

Closed-world is deliberately narrow (§6.6 C28, AER §9.2): there is no
negation-as-failure here.  A CLOSED predicate may only be concluded FALSE from an
*authoritative negative observation* — a complete, scoped query that produced
``f = 1`` with a watermark.  A CLOSED predicate with nothing recorded is UNKNOWN,
exactly like an OPEN one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts.evidence_state import (
    EvidenceEntry,
    ObservationRecord,
    TemporalUse,
    TruthValue,
)
from ..contracts.models import ContractError
from ..contracts.semantic_base import (
    VersionedRef,
    content_hash_of,
    enum_of,
    fields_of,
    flag,
    identifier,
    reject_executable,
    text,
)


def _as_list(value: object, name: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list")
    return list(value)


class WorldAssumption(StrEnum):
    """§6.6: how the absence of a record may be read for this predicate."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ArgumentType(StrEnum):
    """The typed parameter kinds a predicate may take.  No callables, no code."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    REF = "ref"


_TYPE_CHECKS: Mapping[ArgumentType, Any] = {
    ArgumentType.STRING: lambda value: isinstance(value, str),
    ArgumentType.INTEGER: lambda value: isinstance(value, int) and not isinstance(value, bool),
    ArgumentType.NUMBER: lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    ArgumentType.BOOLEAN: lambda value: isinstance(value, bool),
    ArgumentType.REF: lambda value: isinstance(value, str) and bool(value.strip()),
}


@dataclass(frozen=True, slots=True)
class PredicateParameter:
    name: str
    type: ArgumentType
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name, "parameter.name"))
        object.__setattr__(self, "type", enum_of(ArgumentType, self.type, "parameter.type"))
        object.__setattr__(self, "required", flag(self.required, "parameter.required"))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": str(self.type), "required": self.required}

    @classmethod
    def from_json(cls, value: object, name: str = "parameter") -> PredicateParameter:
        data = fields_of(value, name, required=("name", "type"), optional=("required",))
        return cls(name=data["name"], type=data["type"], required=data.get("required", True))


@dataclass(frozen=True, slots=True)
class PredicateSignature:
    """A declared predicate: what it takes, who can observe it, how it is read."""

    predicate_ref: VersionedRef
    parameters: tuple[PredicateParameter, ...] = ()
    world_assumption: WorldAssumption = WorldAssumption.OPEN
    observer_ids: tuple[str, ...] = ()
    temporal_use: TemporalUse = TemporalUse.CURRENT_AT_USE
    authority_scope: str | None = None
    statement: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.predicate_ref, VersionedRef):
            raise ContractError("signature.predicate_ref must be a VersionedRef")
        names = [parameter.name for parameter in self.parameters]
        if len(set(names)) != len(names):
            raise ContractError("signature.parameters must not repeat a name")
        object.__setattr__(
            self,
            "world_assumption",
            enum_of(WorldAssumption, self.world_assumption, "signature.world_assumption"),
        )
        observers = tuple(
            identifier(item, "signature.observer_ids[]") for item in self.observer_ids
        )
        if len(set(observers)) != len(observers):
            raise ContractError("signature.observer_ids must not contain duplicates")
        object.__setattr__(self, "observer_ids", observers)
        object.__setattr__(
            self,
            "temporal_use",
            enum_of(TemporalUse, self.temporal_use, "signature.temporal_use"),
        )
        if self.authority_scope is not None:
            object.__setattr__(
                self,
                "authority_scope",
                identifier(self.authority_scope, "signature.authority_scope"),
            )
        if self.statement is not None:
            object.__setattr__(self, "statement", text(self.statement, "signature.statement"))
        if self.world_assumption is WorldAssumption.CLOSED and not self.observer_ids:
            raise ContractError(
                "a CLOSED-world predicate needs at least one authoritative observer; "
                "without one, absence can never be read as FALSE (§6.6 C28)"
            )

    @property
    def key(self) -> tuple[str, int]:
        return (self.predicate_ref.id, self.predicate_ref.version)

    def to_json(self) -> dict[str, Any]:
        return {
            "predicate_ref": self.predicate_ref.to_json(),
            "parameters": [parameter.to_json() for parameter in self.parameters],
            "world_assumption": str(self.world_assumption),
            "observer_ids": list(self.observer_ids),
            "temporal_use": str(self.temporal_use),
            "authority_scope": self.authority_scope,
            "statement": self.statement,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "predicate_signature") -> PredicateSignature:
        """Rebuild a declaration through the codec.

        The registration rules run again on the way back in — a CLOSED-world
        predicate still needs an observer, and the whole payload still goes through
        :func:`reject_executable` — so a stored or transmitted declaration cannot
        acquire denial powers, or executable content, that registration refused.
        """

        data = fields_of(
            value,
            name,
            required=("predicate_ref",),
            optional=(
                "parameters",
                "world_assumption",
                "observer_ids",
                "temporal_use",
                "authority_scope",
                "statement",
            ),
        )
        reject_executable(data, name)
        return cls(
            predicate_ref=VersionedRef.from_json(data["predicate_ref"], f"{name}.predicate_ref"),
            parameters=tuple(
                PredicateParameter.from_json(item, f"{name}.parameters[]")
                for item in _as_list(data.get("parameters", ()), f"{name}.parameters")
            ),
            world_assumption=data.get("world_assumption", WorldAssumption.OPEN),
            observer_ids=tuple(_as_list(data.get("observer_ids", ()), f"{name}.observer_ids")),
            temporal_use=data.get("temporal_use", TemporalUse.CURRENT_AT_USE),
            authority_scope=data.get("authority_scope"),
            statement=data.get("statement"),
        )


@dataclass(frozen=True, slots=True)
class ArgumentCheck:
    """The result of type-checking one grounded argument set."""

    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    wrong_type: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unknown or self.wrong_type)

    def messages(self) -> tuple[str, ...]:
        out: list[str] = []
        out.extend(f"missing argument {name!r}" for name in self.missing)
        out.extend(f"unknown argument {name!r}" for name in self.unknown)
        out.extend(f"argument {name!r} has the wrong type" for name in self.wrong_type)
        return tuple(out)


class PredicateRegistry:
    """An in-memory, data-only predicate registry.

    Registration is the single place where an untrusted declaration is inspected,
    so every rejection rule lives here rather than in the interpreter: by the time
    ``planning.htn.applicability`` walks a condition, the predicate it names is
    known to be a plain, typed declaration.
    """

    def __init__(self) -> None:
        self._signatures: dict[tuple[str, int], PredicateSignature] = {}

    def register(self, signature: PredicateSignature) -> None:
        if not isinstance(signature, PredicateSignature):
            raise ContractError("register expects a PredicateSignature")
        reject_executable(signature.to_json(), "predicate_signature")
        if signature.key in self._signatures:
            existing = self._signatures[signature.key]
            if existing.predicate_ref.content_hash != signature.predicate_ref.content_hash:
                raise ContractError(
                    f"predicate {signature.key[0]!r} v{signature.key[1]} is already registered "
                    "with different content; publish a new version instead"
                )
            return
        self._signatures[signature.key] = signature

    def resolve(self, ref: VersionedRef) -> PredicateSignature | None:
        signature = self._signatures.get((ref.id, ref.version))
        if signature is None:
            return None
        if signature.predicate_ref.content_hash != ref.content_hash:
            return None
        return signature

    def require(self, ref: VersionedRef) -> PredicateSignature:
        signature = self.resolve(ref)
        if signature is None:
            raise ContractError(
                f"predicate {ref.id!r} v{ref.version} is not registered at that content hash"
            )
        return signature

    def signatures(self) -> tuple[PredicateSignature, ...]:
        return tuple(self._signatures.values())

    def check_arguments(
        self, signature: PredicateSignature, arguments: Mapping[str, Any]
    ) -> ArgumentCheck:
        declared = {parameter.name: parameter for parameter in signature.parameters}
        missing = tuple(
            sorted(
                name
                for name, parameter in declared.items()
                if parameter.required and name not in arguments
            )
        )
        unknown = tuple(sorted(name for name in arguments if name not in declared))
        wrong: list[str] = []
        for name, value in arguments.items():
            parameter = declared.get(name)
            if parameter is None:
                continue
            if not _TYPE_CHECKS[parameter.type](value):
                wrong.append(name)
        return ArgumentCheck(missing=missing, unknown=unknown, wrong_type=tuple(sorted(wrong)))


def proposition_key(signature: PredicateSignature, arguments: Mapping[str, Any]) -> str:
    """AER §8.1: the identity of a proposition is the predicate version plus its typed
    arguments — never natural-language similarity."""

    digest = content_hash_of(
        {
            "predicate": signature.predicate_ref.to_json(),
            "arguments": {key: arguments[key] for key in sorted(arguments)},
        }
    )
    return f"{signature.predicate_ref.id}@{signature.predicate_ref.version}#{digest[:32]}"


def closed_world_denial_admissible(
    signature: PredicateSignature, entry: EvidenceEntry | None
) -> bool:
    """§6.6 C28: may this absence be read as FALSE?

    Only when the domain is closed *and* an authoritative negative observation
    exists.  A closed domain with no record is still UNKNOWN.
    """

    if signature.world_assumption is not WorldAssumption.CLOSED:
        return False
    if entry is None:
        return False
    return entry.authoritative_negative and entry.support.f > 0


def atom_truth(
    signature: PredicateSignature, entry: EvidenceEntry | None, *, now_ms: int | None = None
) -> TruthValue:
    """The four-valued conclusion for one proposition (§6.6, AER §8.3).

    Nothing recorded is UNKNOWN in both worlds; a recorded ``(t, f)`` decides the
    rest, with STALE / REVOKED support degrading to UNKNOWN rather than flipping.
    """

    if entry is None:
        return TruthValue.UNKNOWN
    # A FALSE needs no second guard here: ``EvidenceEntry`` already refuses to hold
    # ``f > 0`` without a recorded counter-observation, so a FALSE that reached this
    # snapshot is backed by one.  ``closed_world_denial_admissible`` remains the
    # separate question of whether a *closed* domain may deny, which the caller asks
    # when it records the provenance of the leaf.
    return entry.truth(now_ms=now_ms)


def authoritative_negative_matches_observer(
    signature: PredicateSignature, observation: ObservationRecord
) -> bool:
    """§6.6 C28: a closed-world denial must come from an observer this predicate lists.

    The coverage claim and the watermark say the query was complete; this says the
    *querier* was one the registry trusts for this predicate.  Without it "complete
    coverage" is a claim anyone could attach to any lookup.
    """

    if not observation.is_authoritative_negative:
        return False
    if signature.world_assumption is not WorldAssumption.CLOSED:
        return False
    if observation.observer_id is None:
        return False
    return observation.observer_id in signature.observer_ids


__all__ = (
    "ArgumentCheck",
    "ArgumentType",
    "PredicateParameter",
    "PredicateRegistry",
    "PredicateSignature",
    "WorldAssumption",
    "atom_truth",
    "authoritative_negative_matches_observer",
    "closed_world_denial_admissible",
    "proposition_key",
)
