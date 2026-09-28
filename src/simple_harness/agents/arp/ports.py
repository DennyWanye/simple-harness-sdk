# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""ARP assembly inputs: the managed root, the activated profile and the trusted caller.

``ArpPorts`` is what an authenticated Host bootstrap hands to ``build_arp_runtime``.
Nothing in it is model-provided.  The session root is a directory the deployment
owns; its ``root.json`` marker (root id + incarnation) is written once by
``bootstrap_root`` and every session directory / marker is derived from it, never
from a caller-supplied path (§7).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .errors import ArpError
from .pins import Pin
from .profile import RuntimeProfile
from .strict import canonical, digest, parse_strict

ROOT_MARKER = "root.json"
SESSIONS_DIR = "sessions"
TRASH_DIR = "trash"
LOCKS_DIR = "locks"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class TrustedCaller:
    """The authenticated principal behind a management command (INTERFACES §2).

    ``principal_ref`` and ``owner_contract_ref`` come from the Host's authenticated
    dispatcher; ``command_receipt_ref`` is the original command receipt the Host
    produced for this action.  A model never constructs one of these.
    """

    principal_ref: Pin
    owner_contract_ref: Pin
    command_receipt_ref: Pin

    def __post_init__(self) -> None:
        self.principal_ref.require_kind("principal")
        self.owner_contract_ref.require_kind("task", "policy")
        self.command_receipt_ref.require_kind("receipt")

    def to_json(self) -> dict[str, Any]:
        return {
            "principal_ref": self.principal_ref.to_json(),
            "owner_contract_ref": self.owner_contract_ref.to_json(),
            "command_receipt_ref": self.command_receipt_ref.to_json(),
        }


@dataclass(frozen=True, slots=True)
class RootIdentity:
    root_id: str
    root_incarnation: str
    directory: Path

    @property
    def sessions(self) -> Path:
        return self.directory / SESSIONS_DIR

    @property
    def trash(self) -> Path:
        return self.directory / TRASH_DIR

    @property
    def locks(self) -> Path:
        return self.directory / LOCKS_DIR

    def resolve_relative(self, relative: str) -> Path:
        """Resolve a stored relative directory and prove it is still inside the root."""

        candidate = (self.directory / relative).resolve()
        root = self.directory.resolve()
        if candidate == root or root not in candidate.parents:
            raise ArpError("REF_OUTSIDE_SCOPE", "session path escapes the managed root")
        return candidate


def bootstrap_root(directory: str | Path, *, root_id: str, incarnation: str | None = None) -> RootIdentity:
    """Create (or verify) the root marker; deployment-level, run once by the installer."""

    base = Path(directory).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    for name in (SESSIONS_DIR, TRASH_DIR, LOCKS_DIR):
        (base / name).mkdir(exist_ok=True)
    marker = base / ROOT_MARKER
    if marker.exists():
        identity = read_root(base)
        if identity.root_id != root_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "root marker names another root_id")
        return identity
    body = {
        "schema_version": 1,
        "root_id": root_id,
        "root_incarnation": incarnation or digest({"root_id": root_id, "at": now_ms(), "pid": os.getpid()})[:32],
        "created_at_ms": now_ms(),
    }
    tmp = marker.with_suffix(".tmp")
    tmp.write_bytes(canonical(body))
    os.replace(tmp, marker)
    return RootIdentity(root_id, str(body["root_incarnation"]), base)


def read_root(directory: str | Path) -> RootIdentity:
    base = Path(directory).expanduser()
    marker = base / ROOT_MARKER
    if not marker.is_file():
        raise ArpError("RUNTIME_CREATION_MARKER_MISSING", f"no {ROOT_MARKER} under the session root")
    body = parse_strict(marker.read_bytes(), max_bytes=4096)
    if (
        not isinstance(body, dict)
        or body.get("schema_version") != 1
        or type(body.get("root_id")) is not str
        or type(body.get("root_incarnation")) is not str
    ):
        raise ArpError("RUNTIME_BINDING_CORRUPT", "root marker is not a v1 marker")
    return RootIdentity(body["root_id"], body["root_incarnation"], base)


ASSURANCE_SUCCESSOR_PENDING = "ASSURANCE_SUCCESSOR_PENDING"


class SkillAcceptancePort(Protocol):
    """The official evaluation acceptance reader (SKILL-CATALOGUE §4): the Assurance
    successor's ``CheckPolicy / EvaluationAcceptance``.  ``verify`` returns the frozen
    acceptance view for ``acceptance_ref`` bound to ``binding`` or raises a named error;
    it never fabricates a PASS."""

    def verify(self, binding: Mapping[str, Any], acceptance_ref: Pin) -> Mapping[str, Any]: ...


class PendingAssuranceAcceptance:
    """Interface freeze (BW09): until the Assurance line lands, no Skill can be admitted.
    Every verify names the pending successor instead of inventing an acceptance."""

    successor = ASSURANCE_SUCCESSOR_PENDING

    def verify(self, binding: Mapping[str, Any], acceptance_ref: Pin) -> Mapping[str, Any]:
        raise ArpError(
            "SKILL_EVALUATION_INCOMPLETE",
            "official evaluation acceptance is not available: Assurance successor pending",
            detail={"successor": ASSURANCE_SUCCESSOR_PENDING, "evaluation_ref": dict(binding["evaluation_ref"]), "acceptance_ref": acceptance_ref.to_json()},
        )


@dataclass(frozen=True, slots=True)
class ScriptRun:
    """One approved SCRIPT execution handed to the original executor (§9.9). ``argv`` keeps
    the whole tokens ``{input_json}`` / ``{output_json}``; the executor substitutes them
    with the files it owns, runs with shell=False in the fixed workspace, and never
    inherits more environment than its own allow-list."""

    skill_ref: Pin
    runner_ref: Pin
    script_path: str
    script_bytes: bytes
    argv: tuple[str, ...]
    input_json: bytes
    workspace_key: str
    timeout_ms: int
    output_limit_bytes: int


@dataclass(frozen=True, slots=True)
class ScriptRunReceipt:
    """What the original executor really observed. ``terminal`` is EXITED (exit code
    known), TIMEOUT (process tree confirmed stopped) or UNKNOWN (no confirmation)."""

    terminal: str
    exit_code: int | None
    output: bytes | None
    receipt_ref: Pin
    stdout_ref: Pin | None = None
    stderr_ref: Pin | None = None
    truncated: bool = False


class ScriptRunnerPort(Protocol):
    """``run`` is required; a runner may also offer ``async run_async(request)``, which the
    model tool awaits instead of moving ``run`` to a worker thread."""

    def run(self, request: ScriptRun) -> ScriptRunReceipt: ...


@dataclass(frozen=True, slots=True)
class ArpPorts:
    """Authenticated assembly inputs for the native runtime plane."""

    root_dir: str | Path
    profile: RuntimeProfile
    activation_receipt: Mapping[str, Any]
    # Real embedding resource availability at assembly (RP-B replaces the flag with
    # the shared resource pool); the §6 truth table decides what creation does.
    embedding_available: bool = False
    clock_ms: Callable[[], int] = now_ms
    # Optional deterministic hook for crash-window tests: called with a point name.
    fault: Callable[[str], None] | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    # RP-B: the deployment's certified counter + model limits (``meter.MeterBinding``).
    # None is refused by the factory: ARP never meters with an uncertified bound.
    meter: Any | None = None
    # RP-B: the real embedding resource (``EmbeddingPort``) and its approved
    # deployment pin; None means the §6 truth table decides (reject / LEXICAL_ONLY).
    embedding: Any | None = None
    embedding_resource_ref: Any | None = None
    # RP-C3: the official evaluation acceptance reader (None → Assurance successor
    # pending: every admit is refused by name) and the approved SCRIPT executor (None →
    # RUNNER_UNAVAILABLE; the SDK never spawns processes itself).
    acceptance: Any | None = None
    script_runner: Any | None = None
    # RP-D2: the Host's authenticated artifact reader (``Pin -> bytes``) for
    # ``HostRequest.payload_ref`` and Skill bundle installs; None → ARTIFACT_UNAVAILABLE.
    artifacts: Any | None = None
    # NEXT-TG-1.0 §10: the original Mission authority's source reader
    # (``mission_sources.MissionSourcePort``).  A MISSION-mode profile refuses creation
    # by name without it; STANDALONE_CHAT never consults it.
    mission_sources: Any | None = None


__all__ = (
    "ASSURANCE_SUCCESSOR_PENDING",
    "ArpPorts",
    "PendingAssuranceAcceptance",
    "ScriptRun",
    "ScriptRunReceipt",
    "ScriptRunnerPort",
    "SkillAcceptancePort",
    "LOCKS_DIR",
    "ROOT_MARKER",
    "RootIdentity",
    "SESSIONS_DIR",
    "TRASH_DIR",
    "TrustedCaller",
    "bootstrap_root",
    "now_ms",
    "read_root",
)
