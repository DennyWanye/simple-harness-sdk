# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host bridge from successful AgentDojo tool returns to Mission knowledge.

Only the evaluation host may promote. Agent-authored Claims stay at most
SUPPORTED under scoped-observation-v2; this path records the tool's returned
bytes as a system observation (data), never as a semantic entailment or new
authority. Failed, unknown, or argument-refused calls do not promote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..contracts import Claim, ClaimStatus
from ..contracts.models import MAX_TEXT, sha256_hex
from ..memory.verified_knowledge import KnowledgeRecord

if TYPE_CHECKING:
    from ..contracts import Task
    from ..orchestrator.commit_service import CommitService


SYSTEM_PROPOSER = "system:agentdojo-tool-observation-v1"


@dataclass(frozen=True, slots=True)
class AgentDojoToolReceipt:
    """Host-held receipt for one returned AgentDojo environment tool call."""

    call_key: str
    function: str
    output: str
    error: str | None
    arguments_sha256: str
    result_sha256: str
    agentdojo_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "call_key": self.call_key,
            "function": self.function,
            "output": self.output,
            "error": self.error,
            "arguments_sha256": self.arguments_sha256,
            "result_sha256": self.result_sha256,
            "agentdojo_version": self.agentdojo_version,
        }


def make_agentdojo_tool_knowledge(
    *,
    mission_id: str,
    task_id: str,
    attempt_id: str,
    result_id: str,
    receipt: AgentDojoToolReceipt,
    now: float,
) -> tuple[Claim, KnowledgeRecord]:
    """Describe exactly one successful tool return, without semantic inference."""
    if receipt.error not in (None, ""):
        raise ValueError("AgentDojo tool errors are not promotable observations")
    body = receipt.output
    content = (
        f"Host observed AgentDojo tool {receipt.function!r} return "
        f"(call {receipt.call_key}): {body}"
    )
    if len(content) > MAX_TEXT:
        raise ValueError("AgentDojo tool observation exceeds knowledge text limit")
    identity = sha256_hex(
        {
            "mission_id": mission_id,
            "result_id": result_id,
            "call_key": receipt.call_key,
            "function": receipt.function,
            "result_sha256": receipt.result_sha256,
        }
    )
    knowledge_id = f"agentdojo-tool:{identity}"
    evidence = (f"tool-run:{receipt.call_key}",)
    basis = {
        "layer": "host_agentdojo_tool_observation",
        "system_observation": True,
        "function": receipt.function,
        "call_key": receipt.call_key,
        "arguments_sha256": receipt.arguments_sha256,
        "result_sha256": receipt.result_sha256,
        "agentdojo_version": receipt.agentdojo_version,
    }
    claim = Claim(
        id=knowledge_id,
        content=content,
        type="tool_observation",
        status=ClaimStatus.VERIFIED,
        source_task=task_id,
        source_attempt=attempt_id,
        evidence=evidence,
        dependencies=(),
        verifier_results=(basis,),
        confidence_metadata={"grade": "verified", "system": True},
        supersedes=None,
        mission_id=mission_id,
        result_id=result_id,
        proposed_by=SYSTEM_PROPOSER,
    )
    record = KnowledgeRecord(
        id=knowledge_id,
        mission_id=mission_id,
        claim_id=knowledge_id,
        content=content,
        type=claim.type,
        status="VERIFIED",
        version=1,
        key=None,
        stance="affirms",
        proposed_by=SYSTEM_PROPOSER,
        source_task=task_id,
        source_attempt=attempt_id,
        source_result=result_id,
        evidence=evidence,
        verifier=basis,
        dependencies=(),
        created_at=now,
        evidence_trust=("trusted",),
    )
    return claim, record


class AgentDojoKnowledgeBridge:
    """Host-owned per-Mission collector for successful AgentDojo tool returns."""

    def __init__(self, service: CommitService, mission_id: str):
        self.service = service
        self.mission_id = mission_id
        self._observations: dict[str, AgentDojoToolReceipt] = {}
        self._auto_observer: Any = None
        # A previous process cannot revalidate private receipts; clear stale
        # system tool knowledge for this Mission when the host reopens.
        service.expire_agentdojo_tool_knowledge(mission_id, reason="host_bridge_reopened")

    def note_returned(self, receipt: AgentDojoToolReceipt) -> None:
        """Remember one returned tool observation. Errors are ignored."""
        if receipt.error not in (None, ""):
            return
        if not receipt.call_key or not receipt.function:
            return
        self._observations[receipt.call_key] = receipt

    def install_auto_observation(self) -> None:
        """Promote matching succeeded tool calls after each accepted Task."""
        if self._auto_observer is not None:
            raise RuntimeError("AgentDojo auto observation already installed")

        def after_accept(task: Task) -> None:
            if task.mission_id != self.mission_id or task.accepted_result_id is None:
                return
            stored = self.service.store.get_result(task.accepted_result_id)
            if stored is None:
                return
            attempt_id = stored.envelope.attempt_id
            for call_key, receipt in tuple(self._observations.items()):
                run = self.service.store.get_tool_call(call_key)
                if (
                    run is None
                    or run["mission_id"] != self.mission_id
                    or run["subject_id"] != attempt_id
                    or run["tool"] != receipt.function
                    or run["outcome"] != "succeeded"
                ):
                    continue
                self.service.promote_agentdojo_tool_observation(
                    self.mission_id,
                    task_id=task.id,
                    result_id=task.accepted_result_id,
                    receipt=receipt,
                )

        self._auto_observer = after_accept
        self.service.on_task_accepted(after_accept)

    def close(self) -> None:
        if self._auto_observer is not None:
            self.service.off_task_accepted(self._auto_observer)
            self._auto_observer = None
        self._observations.clear()


def receipt_from_invoke(
    *,
    call_key: str,
    function: str,
    output: str,
    error: Any,
    arguments: Mapping[str, Any],
    result: Any,
    agentdojo_version: str,
) -> AgentDojoToolReceipt:
    """Build a receipt from the host invoke path (not model-authored)."""
    error_text = None if error in (None, "") else str(error)
    return AgentDojoToolReceipt(
        call_key=call_key,
        function=function,
        output=str(output),
        error=error_text,
        arguments_sha256=sha256_hex(dict(arguments)),
        result_sha256=sha256_hex(
            result
            if isinstance(result, (dict, list, str, int, float, bool, type(None)))
            else str(result)
        ),
        agentdojo_version=agentdojo_version,
    )


__all__ = (
    "AgentDojoKnowledgeBridge",
    "AgentDojoToolReceipt",
    "SYSTEM_PROPOSER",
    "make_agentdojo_tool_knowledge",
    "receipt_from_invoke",
)
