# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Trace (§23.1, theory 12-13, S6-09, plan D6-10 / D6-10').

The trace root is the Mission (``trace_id = ids.trace_id(mission_id)``, the same value
every event envelope carries); every Attempt is a span.  Each span names what produced
its result and with which versions: runtime profile, requested and echoed model, prompt,
context, retrieval, allocator, router and the version of every verification layer that
ran — "没有版本信息，实验往往无法复现" (theory 12-13).  Service calls (Planner, Manager,
Critic) are listed with their own route and prompt version."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import ids
from ..storage.store import Store

TRACE_VERSION = "trace-v1"


def trace(
    store: Store, mission_id: str, *, echoes: Mapping[str, Sequence[str]] | None = None
) -> dict[str, Any]:
    trace_id = ids.trace_id(mission_id)
    spans: list[dict[str, Any]] = []
    for task in store.list_tasks(mission_id):
        for attempt in store.list_attempts(task.id):
            intent = store.get_intent_for_subject(attempt.id)
            config: Mapping[str, Any] = intent.config if intent is not None else {}
            stored = store.find_result_for_attempt(attempt.id)
            layers = [] if stored is None else store.list_verifications(stored.envelope.id)
            echoed = sorted((echoes or {}).get(attempt.id, ()))
            spans.append(
                {
                    "trace_id": trace_id,
                    "mission_id": mission_id,
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "agent_id": attempt.agent_id or (None if intent is None else intent.agent_id),
                    "turn_id": attempt.turn_id,
                    "role": attempt.role,
                    "status": str(attempt.status),
                    "retry_of": attempt.retry_of,
                    "result_id": None if stored is None else stored.envelope.id,
                    "verdict": None if stored is None else stored.verdict,
                    "model_version": {
                        "runtime_profile_id": attempt.runtime_profile_id,
                        "requested_model": attempt.model,
                        "echoed_models": echoed or None,
                    },
                    "prompt_version": attempt.prompt_version,
                    "context_version": attempt.context_version,
                    "retrieval_version": config.get("retrieval_version"),
                    "context_builder_version": config.get("context_builder_version"),
                    "allocator_version": (config.get("allocation") or {}).get("allocator_version"),
                    "router_version": (config.get("routing") or {}).get("router_version"),
                    "verifier_version": {
                        layer["layer"]: (layer.get("detail") or {}).get("verifier_version")
                        for layer in layers
                        if layer["status"] not in {"NOT_REQUIRED", "SKIPPED"}
                    },
                    "knowledge_seen": list(config.get("knowledge") or []),
                }
            )
    services = [
        {
            "trace_id": trace_id,
            "kind": intent.kind,
            "subject_id": intent.subject_id,
            "agent_id": intent.agent_id,
            "state": intent.state,
            "runtime_profile_id": intent.config.get("runtime_profile_id", "default"),
            "model": intent.config.get("model"),
            "prompt_version": intent.config.get("prompt_version"),
            "context_version": intent.config.get("context_version"),
        }
        for intent in store.list_intents(
            "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
        )
        if intent.mission_id == mission_id and intent.kind != "attempt"
    ]
    actions = [  # step 7 (D7-11): decision receipt → hand-off → service receipt
        {
            "trace_id": trace_id,
            "action_key": a["action_key"],
            "action_id": a["action_id"],
            "version": a["version"],
            "state": a["state"],
            "task_id": a.get("task_id"),
            "result_id": a.get("result_id"),
            "idempotency_key": a.get("idempotency_key"),
            "approval_request_id": a.get("approval_request_id"),
            "decision_receipts": list(a.get("decision_receipts") or []),
            "handoffs": a.get("handoffs", 0),
            "receipt_hash": (a.get("receipt") or {}).get("receipt_hash"),
        }
        for a in store.list_actions(mission_id)
    ]
    return {
        "trace_id": trace_id,
        "mission_id": mission_id,
        "version": TRACE_VERSION,
        "spans": spans,
        "services": services,
        "actions": actions,
    }


__all__ = ("TRACE_VERSION", "trace")
