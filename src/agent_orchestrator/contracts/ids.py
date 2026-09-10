# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Stable identities (plan D3).  Every id is derived from durable inputs so a replay
of the same command produces the same id (§17.4 idempotency)."""

from __future__ import annotations

from .models import sha256_hex


def mission_id(tenant_id: str, idempotency_key: str) -> str:
    return (
        "mission-" + sha256_hex({"tenant_id": tenant_id, "idempotency_key": idempotency_key})[:16]
    )


def task_id(mission: str, ordinal: int) -> str:
    return f"{mission}:task-{ordinal}"


def attempt_id(task: str, ordinal: int) -> str:
    return f"{task}:attempt-{ordinal}"


def result_id(attempt: str, turn_id: str, result_hash: str) -> str:
    return (
        "result-"
        + sha256_hex({"attempt_id": attempt, "turn_id": turn_id, "result_hash": result_hash})[:16]
    )


def claim_id(result: str, index: int) -> str:
    return f"{result}:claim-{index}"


def artifact_id(attempt: str, path: str, content_hash: str) -> str:
    return (
        "artifact-"
        + sha256_hex({"attempt_id": attempt, "path": path, "content_hash": content_hash})[:16]
    )


def commit_id(proposal: object, base_version: object) -> str:
    return "commit-" + sha256_hex({"proposal": proposal, "base_version": base_version})[:16]


def event_id(idempotency_key: str) -> str:
    return "event-" + sha256_hex(idempotency_key)[:16]


def intent_id(kind: str, subject: str) -> str:
    return f"intent-{kind}-{subject}"


def trace_id(mission: str) -> str:
    return "trace-" + sha256_hex(mission)[:16]


__all__ = (
    "artifact_id",
    "attempt_id",
    "claim_id",
    "commit_id",
    "event_id",
    "intent_id",
    "mission_id",
    "result_id",
    "task_id",
    "trace_id",
)
