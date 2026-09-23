# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure deletion-recovery decision over *actual* path observations (JOBS-LIFECYCLE J6, R1).

``recovery_action`` never touches the file system; the caller performs exactly the
one action it names under the session FileGuard and records a real receipt.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Mapping

from .errors import ArpError

Json = Mapping[str, object]

PHASES = ("DRAINING", "RENAME_PENDING", "RENAMED", "DELETE_CONFIRMED")
BLOCK_CODES = ("DUAL_DIRECTORY", "MARKER_MISMATCH", "DELETE_STATE_AMBIGUOUS", "FILE_BUSY", "IO_ERROR")


def need(value: object, code: str = "PURGE_STATE_INVALID") -> None:
    if not value:
        raise ArpError(code)


def validate_relative_directory(path: object) -> str:
    need(isinstance(path, str) and path)
    text = str(path)
    parts = PurePosixPath(text).parts
    need(not text.startswith("/") and "\\" not in text and ":" not in text)
    need(".." not in parts and "." not in text.split("/") and str(PurePosixPath(text)) == text)
    return text


def validate_progress(p: Json) -> None:
    for field in ("source_relative_directory", "trash_relative_directory"):
        validate_relative_directory(p[field])
    need(p["source_relative_directory"] != p["trash_relative_directory"])
    phase = p["phase"]
    need(phase in PHASES)
    if phase in ("RENAMED", "DELETE_CONFIRMED"):
        need(p["rename_receipt_ref"] is not None)
    if phase == "DELETE_CONFIRMED":
        need(p["delete_receipt_ref"] is not None)
    else:
        need(p["delete_receipt_ref"] is None)
    if phase in ("DRAINING", "RENAME_PENDING"):
        need(p["rename_receipt_ref"] is None)
    block = p["blocking"]
    if block:
        need(p["inspection_receipt_ref"] is not None)
        expected_mode = "SAME_DESTROY_BACKOFF" if block["code"] == "FILE_BUSY" else "MANUAL"  # type: ignore[index]
        need(block["retry_mode"] == expected_mode)  # type: ignore[index]
        need((block["retry_at_ms"] is not None) == (block["retry_mode"] == "SAME_DESTROY_BACKOFF"))  # type: ignore[index]


def recovery_action(progress: Json, observation: Json) -> str:
    """Map (persisted phase, two-directory observation) to the single allowed action."""

    validate_progress(progress)
    for key in ("session_id", "destroy_command_id", "control_generation", "expected_marker_hash"):
        if progress[key] != observation[key]:
            raise ArpError("PURGE_IDENTITY_MISMATCH", field_path=key)
    source, trash = observation["source_state"], observation["trash_state"]
    if "MISMATCH" in (source, trash):
        return "BLOCK_MARKER_MISMATCH"
    if source != "ABSENT" and trash != "ABSENT":
        return "BLOCK_DUAL_DIRECTORY"
    phase = progress["phase"]
    if phase == "DRAINING":
        return "WAIT_DISPOSAL_PROOF"
    if source == "MATCH" and trash == "ABSENT":
        return "RENAME_EXACT" if phase == "RENAME_PENDING" else "BLOCK_DELETE_STATE_AMBIGUOUS"
    if source == "ABSENT" and trash == "MATCH":
        if phase == "RENAME_PENDING":
            return "RECORD_RECOVERED_RENAME"
        return "DELETE_EXACT" if phase == "RENAMED" else "BLOCK_DELETE_STATE_AMBIGUOUS"
    if source == "ABSENT" and trash == "ABSENT":
        if phase == "RENAMED":
            return "RECORD_DELETION_ABSENCE"
        if phase == "DELETE_CONFIRMED":
            return "FINALIZE_SAME_DESTROY"
    return "BLOCK_DELETE_STATE_AMBIGUOUS"


def can_rebuild(session: Json) -> bool:
    """Ordinary same-root rebuild is allowed only without any destroy identity."""

    return (
        session["state"] == "QUARANTINED"
        and session.get("destroy_command_id") is None
        and session.get("purge_progress") is None
    )


__all__ = (
    "BLOCK_CODES",
    "PHASES",
    "can_rebuild",
    "recovery_action",
    "validate_progress",
    "validate_relative_directory",
)
