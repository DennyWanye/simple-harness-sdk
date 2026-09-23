# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure ARP-EXEC-1.1.1 runtime rules (the executable specification, production port).

Budget capacity (§4), the unique long-turn allocation algorithm (§4.1), stable RRF
fusion (§5.3, R4), cursor checks (§5.2), request disposition (§3.3), embedding
activation truth table (§6), skill path safety (§9), dependency locks (§9.5), the
seven-collection disposal proof (§7) and SQLite statement splitting (§11).

Nothing here touches a model, the network, a database or authorisation; callers
own those.  Every failure is an ``ArpError`` with a catalogue code.
"""

from __future__ import annotations

import math
import re
import sqlite3
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Iterator, Mapping, Sequence

from .errors import ArpError
from .strict import digest, nonnegative_int

GROUP_KINDS = (
    "USER_ANCHOR",
    "CLOSED_TOOL",
    "OPEN_TAIL",
    "TERMINAL_ANSWER",
    "OPAQUE_REQUIRED",
    "HISTORY_MESSAGE",
)
INPUT_SCOPES = ("WIRE_ONLY", "WIRE_PLUS_PRIOR")
MAX_SHRINK_STEPS = 256


# ---- §4 budget ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """U_wire inputs: C_config, C_model_combined, C_model_input, O, S, H, floor, ceiling, P."""

    configured_total: int
    model_total: int | None
    model_input: int
    model_output: int
    output: int
    safety: int
    headroom: int
    recent_floor: int
    recall_ceiling: int
    prior: int = 0
    input_scope: str = "WIRE_ONLY"

    def capacity(self) -> int:
        for value in (
            self.configured_total,
            self.model_input,
            self.model_output,
            self.output,
            self.safety,
            self.headroom,
            self.recent_floor,
            self.recall_ceiling,
            self.prior,
        ):
            nonnegative_int(value)
        if (
            min(self.configured_total, self.model_input, self.model_output, self.output) <= 0
            or self.output > self.model_output
        ):
            raise ArpError("INVALID_OUTPUT_RESERVE")
        if self.input_scope not in INPUT_SCOPES:
            raise ArpError("INVALID_BUDGET")
        caps = [
            self.configured_total - self.prior - self.output - self.safety - self.headroom,
            self.model_input
            - self.safety
            - self.headroom
            - (self.prior if self.input_scope == "WIRE_PLUS_PRIOR" else 0),
        ]
        if self.model_total is not None:
            if nonnegative_int(self.model_total) == 0:
                raise ArpError("INVALID_MODEL_TOTAL")
            caps.append(self.model_total - self.prior - self.output - self.safety - self.headroom)
        capacity = min(caps)
        if capacity <= 0:
            raise ArpError("NO_INPUT_BUDGET")
        return capacity


# ---- §4.1 allocation ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Group:
    id: str
    turn_id: str
    seq: int
    tokens: int
    kind: str = "CLOSED_TOOL"
    mandatory: bool = False
    closed: bool = True
    call_ids: tuple[str, ...] = ()
    result_call_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Turn:
    id: str
    completed: bool
    required_groups: frozenset[str]


@dataclass(frozen=True, slots=True)
class Recall:
    id: str
    groups: frozenset[str]
    tokens: int


@dataclass(frozen=True, slots=True)
class Selection:
    recent: tuple[Group, ...]
    recalled: tuple[Recall, ...]
    used: int
    input_budget: int
    complete_turn_ids: tuple[str, ...]
    count_complete: bool

    @property
    def recent_complete_turns(self) -> int:
        return len(self.complete_turn_ids)


def validate_groups(groups: Sequence[Group]) -> None:
    if len({g.id for g in groups}) != len(groups):
        raise ArpError("DUPLICATE_GROUP")
    if len({g.seq for g in groups}) != len(groups):
        raise ArpError("STRUCTURE_INVALID")
    for group in groups:
        nonnegative_int(group.tokens, "BAD_GROUP_SIZE")
        if group.kind not in GROUP_KINDS:
            raise ArpError("ENUM", field_path="kind")
        if group.kind in ("OPEN_TAIL", "OPAQUE_REQUIRED") and not group.mandatory:
            raise ArpError("STATE_COMBINATION_INVALID")
        if group.kind == "CLOSED_TOOL" and (
            not group.closed
            or len(set(group.call_ids)) != len(group.call_ids)
            or len(set(group.result_call_ids)) != len(group.result_call_ids)
            or set(group.call_ids) != set(group.result_call_ids)
        ):
            raise ArpError("STATE_COMBINATION_INVALID")


def allocate(
    budget: Budget,
    fixed: int,
    groups: Sequence[Group],
    candidates: Sequence[Recall],
    *,
    turns: Sequence[Turn],
    current_turn_id: str,
    enumeration_complete: bool = True,
) -> Selection:
    """The single §4.1 algorithm: mandatory → recent suffix → F → grow G backwards."""

    capacity = budget.capacity()
    nonnegative_int(fixed, "INVALID_FIXED")
    validate_groups(groups)
    if len({r.id for r in candidates}) != len(candidates):
        raise ArpError("DUPLICATE_RECALL")
    for recall in candidates:
        nonnegative_int(recall.tokens, "BAD_RECALL")
        if not recall.groups:
            raise ArpError("BAD_RECALL")
    ordered = tuple(sorted(groups, key=lambda g: g.seq))
    mandatory = [g for g in ordered if g.mandatory]
    if not any(
        g.turn_id == current_turn_id and g.kind == "USER_ANCHOR" and g.mandatory for g in ordered
    ):
        raise ArpError("STATE_COMBINATION_INVALID", "current turn needs a mandatory USER_ANCHOR")
    optional = [g for g in ordered if not g.mandatory]
    recent = list(mandatory)
    used = sum(g.tokens for g in recent)
    if fixed + used > capacity:
        raise ArpError("REQUIRED_CONTEXT_TOO_LARGE")
    # Step 3: protect recent_min capacity with the most recent contiguous suffix.
    start = len(optional)
    floor = max(used, min(budget.recent_floor, capacity - fixed))
    while start > 0 and used + optional[start - 1].tokens <= floor:
        start -= 1
        recent.append(optional[start])
        used += optional[start].tokens
    # Step 4: recall candidates that do not overlap the selected groups.
    ids = {g.id for g in recent}
    recall_max = min(budget.recall_ceiling, max(0, capacity - fixed - used))
    recalled: list[Recall] = []
    recall_used = 0
    for recall in candidates:
        if recall.groups & ids:
            continue
        if recall_used + recall.tokens <= recall_max:
            recalled.append(recall)
            recall_used += recall.tokens
    # Step 5: grow G backwards; an older group evicts the F items it overlaps.
    while start > 0:
        group = optional[start - 1]
        trial = [r for r in recalled if group.id not in r.groups]
        trial_tokens = sum(r.tokens for r in trial)
        if fixed + used + group.tokens + trial_tokens > capacity:
            break
        start -= 1
        recent.append(group)
        used += group.tokens
        recalled = trial
    ids = {g.id for g in recent}
    # Step 6: N counts only historic Turns whose required groups are all selected.
    complete = tuple(
        sorted(
            t.id
            for t in turns
            if t.completed and t.id != current_turn_id and t.required_groups
            and t.required_groups <= ids
        )
    )
    total = fixed + used + sum(r.tokens for r in recalled)
    if total > capacity:
        raise ArpError("INTERNAL_CONTEXT_OVERFLOW")
    return Selection(
        tuple(sorted(recent, key=lambda g: g.seq)),
        tuple(recalled),
        total,
        capacity,
        complete,
        enumeration_complete,
    )


def verify_final_count(selection: Selection, count: int, request_bytes: int, max_bytes: int) -> None:
    nonnegative_int(count, "BAD_TOKEN_RECEIPT")
    if request_bytes > max_bytes:
        raise ArpError("REQUEST_BYTES_LIMIT")
    if count > selection.input_budget:
        raise ArpError("FINAL_CONTEXT_OVERFLOW")


# ---- §6 vectors ---------------------------------------------------------------


def validate_vector(values: Iterable[float], dim: int) -> tuple[float, ...]:
    """SCALED_L2_F32_V1: scale by max-abs, L2 normalise, round-trip through f32."""

    vector = tuple(values)
    if type(dim) is not int or dim < 1 or len(vector) != dim:
        raise ArpError("EMBEDDING_DIM_MISMATCH")
    if any(type(a) not in (float, int) or not math.isfinite(a) for a in vector):
        raise ArpError("INVALID_EMBEDDING")
    scale = max(abs(a) for a in vector)
    if scale == 0:
        raise ArpError("ZERO_EMBEDDING")
    scaled = [a / scale for a in vector]
    norm = math.sqrt(math.fsum(a * a for a in scaled))
    raw = struct.pack("<" + str(dim) + "f", *(a / norm for a in scaled))
    out = struct.unpack("<" + str(dim) + "f", raw)
    if not all(math.isfinite(a) for a in out) or not any(a != 0 for a in out):
        raise ArpError("INVALID_EMBEDDING")
    return out


# ---- §5 history slices, fusion, cursors ---------------------------------------


def utf8_slice(text: str, start: int, max_bytes: int) -> tuple[str, int, bool]:
    """Return ``(slice, end, complete)`` on UTF-8 boundaries; never split a code point."""

    raw = text.encode("utf-8")
    if (
        type(start) is not int
        or not 0 <= start <= len(raw)
        or type(max_bytes) is not int
        or max_bytes < 4
    ):
        raise ArpError("HISTORY_BYTE_RANGE_INVALID")
    try:
        raw[:start].decode("utf-8")
    except UnicodeError as error:
        raise ArpError("HISTORY_BYTE_RANGE_INVALID") from error
    end = min(start + max_bytes, len(raw))
    while end > start:
        try:
            piece = raw[start:end].decode("utf-8")
        except UnicodeError:
            end -= 1
            continue
        return piece, end, end == len(raw)
    raise ArpError("HISTORY_BYTE_RANGE_INVALID")


RRF_WEIGHTS: Mapping[str, float] = {"exact": 1.0, "words": 0.6, "trigram": 0.5, "vector": 0.7}
RRF_K = 60


def stable_fused_topk(
    scores: Mapping[str, float], stable_keys: Mapping[str, tuple], limit: int
) -> list[tuple[str, float]]:
    return sorted(scores.items(), key=lambda kv: (-kv[1], stable_keys[kv[0]]))[:limit]


def rrf(
    channels: Mapping[str, Sequence[tuple[str, float]]],
    max_candidates: int = 128,
    *,
    stable_keys: Mapping[str, tuple],
    channel_limit: int = 128,
) -> list[tuple[str, float]]:
    """Weighted reciprocal rank fusion; every tie uses the same stable five-tuple key."""

    if (
        type(channel_limit) is not int
        or not 1 <= channel_limit <= 512
        or type(max_candidates) is not int
        or not 1 <= max_candidates <= 512
    ):
        raise ArpError("ARRAY_LIMIT")
    scores: dict[str, float] = {}
    for name in sorted(channels):
        hits = channels[name]
        if name not in RRF_WEIGHTS:
            raise ArpError("ENUM", field_path="channel")
        if any(key not in stable_keys for key, _ in hits):
            raise ArpError("SOURCE_UNAVAILABLE")
        if len({key for key, _ in hits}) != len(hits):
            raise ArpError("DUPLICATE_ITEM")
        ranked = sorted(hits, key=lambda x: (-x[1], stable_keys[x[0]]))[:channel_limit]
        for rank, (key, _) in enumerate(ranked, 1):
            scores[key] = scores.get(key, 0.0) + RRF_WEIGHTS[name] / (RRF_K + rank)
    return stable_fused_topk(scores, stable_keys, max_candidates)


def check_cursor(stored: Mapping[str, object], current: Mapping[str, object]) -> None:
    for key in ("owner", "session", "control_generation", "purpose", "query_hash", "request_hash"):
        if stored[key] != current[key]:
            raise ArpError(
                "CURSOR_SCOPE_MISMATCH"
                if key in ("owner", "session", "purpose")
                else "CURSOR_REQUEST_MISMATCH"
            )
    if current["now_ms"] >= stored["expires_at_ms"]:  # type: ignore[operator]
        raise ArpError("CURSOR_EXPIRED")
    for key in ("index_generation", "authority_hash", "root_incarnation"):
        if stored[key] != current[key]:
            raise ArpError("CURSOR_STALE")
    # A newer append commit is deliberately not compared: the frozen upper_commit holds.


# ---- §3.3 request disposition --------------------------------------------------


def disposition(
    phase: str,
    *,
    safety_changed: bool = False,
    settings_changed: bool = False,
    no_send_final: bool = False,
) -> str:
    del settings_changed  # settings only affect the next unfrozen request (§3.3)
    if phase in ("PREPARED", "RESERVED", "WAITING_FOR_SLOT"):
        if safety_changed:
            return "CANCEL_UNSENT" if no_send_final else "REQUEST_UNSENT_TERMINATION_REQUIRED"
        return "KEEP_FROZEN"
    if phase in ("HANDED_OFF", "UNKNOWN"):
        return "RECONCILE_ORIGINAL"
    if phase in ("SUCCEEDED", "FAILED"):
        return "COLLECT_ORIGINAL"
    raise ArpError("REQUEST_PHASE_UNMAPPED")


# ---- §6 embedding activation ---------------------------------------------------


def embedding_mode(required: bool, degrade: bool, *, creating: bool, available: bool) -> str:
    if type(required) is not bool or type(degrade) is not bool or not (required or degrade):
        raise ArpError("STATE_COMBINATION_INVALID")
    if available:
        return "HYBRID"
    if creating and required:
        return "REJECT_CREATION"
    return "LEXICAL_ONLY" if degrade else "UNAVAILABLE"


# ---- §9 skills ------------------------------------------------------------------

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def safe_skill_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path or ":" in path:
        raise ArpError("SKILL_PATH_INVALID")
    if path != unicodedata.normalize("NFC", path):
        raise ArpError("SKILL_PATH_NONCANONICAL")
    parts = path.split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts) or PurePosixPath(
        path
    ).is_absolute():
        raise ArpError("SKILL_PATH_INVALID")
    if len(path.encode()) > 1024 or any(len(p.encode()) > 255 for p in parts):
        raise ArpError("SKILL_PATH_INVALID")
    if any(p.split(".")[0].upper() in _WINDOWS_RESERVED for p in parts):
        raise ArpError("SKILL_PATH_RESERVED")
    return path


def validate_bundle_paths(paths: Iterable[str]) -> None:
    seen: set[str] = set()
    for path in paths:
        key = safe_skill_path(path).casefold()
        if key in seen:
            raise ArpError("SKILL_PATH_COLLISION")
        seen.add(key)


def effective_permissions(*sets: frozenset[str]) -> frozenset[str]:
    if not sets:
        raise ArpError("AUTHORITY_SOURCE_MISSING")
    out = sets[0]
    for item in sets[1:]:
        out = out & item
    return out


def choose_provider(candidates: Sequence[Mapping[str, object]], required: frozenset[str]) -> str:
    """Hard filter, then approved priority descending, then stable id."""

    necessary = ("registered", "configured", "healthy", "authorized", "compatible")
    eligible = [
        c
        for c in candidates
        if all(c.get(k) is True for k in necessary)
        and required <= frozenset(c.get("features", ()))  # type: ignore[arg-type]
    ]
    if not eligible:
        raise ArpError("CAPABILITY_UNAVAILABLE")
    for candidate in eligible:
        if type(candidate.get("priority")) is not int:
            raise ArpError("TYPE", field_path="priority")
    return str(sorted(eligible, key=lambda c: (-c["priority"], c["id"]))[0]["id"])  # type: ignore[operator]


def tool_route(effect_class: str, exposed: bool, actual_grant: bool) -> str:
    if not exposed:
        raise ArpError("TOOL_NOT_EXPOSED")
    if not actual_grant:
        raise ArpError("AUTHORIZATION_REQUIRED")
    routes = {
        "READ_ONLY": "ORIGINAL_TOOL_EXECUTOR",
        "SANDBOX_WRITE": "ORIGINAL_SANDBOX_EXECUTOR",
        "EXTERNAL_EFFECT": "ORIGINAL_OPERATION_INTENT",
    }
    if effect_class not in routes:
        raise ArpError("EFFECT_CLASS_UNKNOWN")
    return routes[effect_class]


def dependency_lock(
    nodes: Mapping[str, tuple[int, str]],
    edges: Sequence[tuple[str, str]],
    max_depth: int = 16,
) -> None:
    if len(nodes) > 128:
        raise ArpError("ARRAY_LIMIT")
    adjacency: dict[str, list[str]] = {k: [] for k in nodes}
    for a, b in edges:
        if a not in nodes or b not in nodes:
            raise ArpError("DEPENDENCY_UNRESOLVED")
        adjacency[a].append(b)

    def walk(node: str, path: tuple[str, ...]) -> None:
        if node in path:
            raise ArpError("DEPENDENCY_CYCLE")
        if len(path) >= max_depth:
            raise ArpError("ARRAY_LIMIT")
        for child in adjacency[node]:
            walk(child, path + (node,))

    for node in nodes:
        walk(node, ())


# ---- §7 disposal ----------------------------------------------------------------

DISPOSAL_KINDS = frozenset(
    {"TURNS", "CALLS", "UNKNOWN", "PENDING_IMPORTS", "INDEX_WRITERS", "READ_HANDLES", "TEMP_ROOTS"}
)


def complete_disposal(collections: Sequence[Mapping[str, object]]) -> bool:
    """True only when all seven witnessed collections are complete and empty."""

    if len(collections) != 7 or {c["kind"] for c in collections} != DISPOSAL_KINDS:
        raise ArpError("RETENTION_PROOF_INCOMPLETE")
    for witness in collections:
        refs = witness["refs"]
        if (
            witness["complete"] is not True
            or witness["count"] != len(refs)  # type: ignore[arg-type]
            or witness["set_hash"] != digest(refs)
        ):
            raise ArpError("RETENTION_PROOF_INCOMPLETE")
    return all(c["count"] == 0 for c in collections)


# ---- §11 SQL ----------------------------------------------------------------------

_SQL_TRAILER = re.compile(r"(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)


def sql_statements(ddl: str, *, allow_unterminated_final: bool = False) -> Iterator[str]:
    """Split DDL on real statement boundaries (never ``split(';')``)."""

    buffer = ""
    for char in ddl:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip() and not _SQL_TRAILER.fullmatch(buffer):
        if allow_unterminated_final and sqlite3.complete_statement(buffer + ";"):
            yield buffer + ";"
        else:
            raise ArpError("SQL_INCOMPLETE")


__all__ = (
    "Budget",
    "DISPOSAL_KINDS",
    "GROUP_KINDS",
    "Group",
    "MAX_SHRINK_STEPS",
    "RRF_WEIGHTS",
    "Recall",
    "Selection",
    "Turn",
    "allocate",
    "check_cursor",
    "choose_provider",
    "complete_disposal",
    "dependency_lock",
    "disposition",
    "effective_permissions",
    "embedding_mode",
    "rrf",
    "safe_skill_path",
    "sql_statements",
    "stable_fused_topk",
    "tool_route",
    "utf8_slice",
    "validate_bundle_paths",
    "validate_groups",
    "validate_vector",
    "verify_final_count",
)
