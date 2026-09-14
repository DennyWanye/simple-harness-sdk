# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host bridge from current public AppWorld GETs to Mission knowledge.

Only the independent episode port can authorize a promotion. The bridge exposes
the existing core knowledge read path to the next Task and revokes projections
before episode mutations; it never interprets agent shell output as evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from ..context.knowledge_tools import read_knowledge_tool
from ..contracts import Claim, ClaimStatus
from ..contracts.models import MAX_TEXT
from ..memory.verified_knowledge import KnowledgeRecord
from .appworld import AppWorldEpisode
from .appworld_api_observations import AppWorldAPIReceipt, canonical, project

if TYPE_CHECKING:
    from ..orchestrator.commit_service import CommitService


SYSTEM_PROPOSER = "system:appworld-public-api-v1"


def make_appworld_knowledge(
    *, mission_id: str, task_id: str, attempt_id: str, result_id: str,
    receipt: AppWorldAPIReceipt, response: Mapping[str, Any], now: float,
) -> tuple[Claim, KnowledgeRecord]:
    """Describe exactly one allowlisted API response, without semantic inference."""
    safe = project(receipt.app, receipt.api, dict(response))
    body = canonical(safe).decode("utf-8")
    content = f"Host observed {receipt.app}.{receipt.api} public response: {body}"
    if len(content) > MAX_TEXT:
        raise ValueError("public API observation exceeds knowledge text limit")
    identity = sha256(canonical({
        "mission_id": mission_id, "result_id": result_id,
        "receipt_id": receipt.receipt_id,
    })).hexdigest()
    knowledge_id = f"appworld-api:{identity}"
    evidence = (f"appworld-api:{receipt.receipt_id}",)
    basis = {
        "layer": "host_public_api_observation",
        "system_observation": True,
        "run_id": receipt.run_id,
        "appworld_task_id": receipt.task_id,
        "episode_id": receipt.episode_id,
        "world_version": receipt.world_version,
        "app": receipt.app,
        "api": receipt.api,
        "receipt_id": receipt.receipt_id,
        "response_sha256": receipt.response_sha256,
        "service_identity_sha256": sha256(
            (receipt.service_identity or "").encode("utf-8")
        ).hexdigest(),
    }
    claim = Claim(
        id=knowledge_id, content=content, type="api_observation",
        status=ClaimStatus.VERIFIED, source_task=task_id,
        source_attempt=attempt_id, evidence=evidence, dependencies=(),
        verifier_results=(basis,), confidence_metadata={"grade": "verified", "system": True},
        supersedes=None, mission_id=mission_id, result_id=result_id,
        proposed_by=SYSTEM_PROPOSER,
    )
    record = KnowledgeRecord(
        id=knowledge_id, mission_id=mission_id, claim_id=knowledge_id,
        content=content, type=claim.type, status="VERIFIED", version=1,
        key=None, stance="affirms", proposed_by=SYSTEM_PROPOSER,
        source_task=task_id, source_attempt=attempt_id, source_result=result_id,
        evidence=evidence, verifier=basis, dependencies=(), created_at=now,
        evidence_trust=("trusted",),
    )
    return claim, record


class AppWorldKnowledgeBridge:
    """Host-owned per-Mission connection for serial AppWorld Tasks."""

    def __init__(self, service: CommitService, mission_id: str, episode: AppWorldEpisode):
        self.service, self.mission_id, self.episode = service, mission_id, episode
        self._observations: dict[str, tuple[AppWorldAPIReceipt, dict[str, str | None]]] = {}
        self._auto_observer: Any = None
        # A previous process cannot revalidate its private receipt registry.
        service.expire_appworld_api_knowledge(
            mission_id, episode_id=episode.episode_id,
            reason="host_bridge_reopened",
        )
        service.bind_host_knowledge_sync(mission_id, self.sync)
        episode.on_world_change(self._on_world_change)

    def _on_world_change(self, next_version: int) -> None:
        self.service.expire_appworld_api_knowledge(
            self.mission_id, episode_id=self.episode.episode_id,
            reason=f"world_version:{next_version}",
        )
        self._observations.clear()

    def install_auto_observation(self) -> None:
        """Observe after each accepted Task, before the scheduler starts its dependents."""
        if self._auto_observer is not None:
            raise RuntimeError("AppWorld auto observation already installed")

        def after_accept(task: Any) -> None:
            if task.mission_id != self.mission_id:
                return
            receipt, response = self.episode.observe_public_api(
                "supervisor", "show_active_task"
            )
            self.promote(task_id=task.id, result_id=task.accepted_result_id,
                         receipt=receipt, response=response)

        self._auto_observer = after_accept
        self.service.on_task_accepted(after_accept)

    def close(self) -> None:
        self.service.expire_appworld_api_knowledge(
            self.mission_id, episode_id=self.episode.episode_id,
            reason="host_bridge_closed",
        )
        if self._auto_observer is not None:
            self.service.off_task_accepted(self._auto_observer)
            self._auto_observer = None
        self.service.unbind_host_knowledge_sync(self.mission_id, self.sync)
        self.episode.off_world_change(self._on_world_change)

    def promote(self, *, task_id: str, result_id: str,
                receipt: AppWorldAPIReceipt,
                response: Mapping[str, Any]) -> KnowledgeRecord:
        record = self.service.promote_appworld_api_observation(
            self.mission_id, task_id=task_id, result_id=result_id,
            episode=self.episode, receipt=receipt, response=response,
        )
        self._observations[record.id] = (receipt, project(receipt.app, receipt.api, dict(response)))
        return record

    def sync(self) -> None:
        """Recheck service identity before a later Task sees any cached fact."""
        stale = []
        for knowledge_id, (receipt, response) in tuple(self._observations.items()):
            try:
                current = self.episode.verify_api_observation(
                    receipt, run_id=receipt.run_id, task_id=receipt.task_id,
                    episode_id=receipt.episode_id, world_version=receipt.world_version,
                    app=receipt.app, api=receipt.api, response=response, blocking=False,
                )
            except Exception:
                # An unavailable Host port cannot preserve a cached trusted fact.
                current = False
            if current is not True:
                stale.append(knowledge_id)
        if stale:
            self.service.expire_appworld_api_knowledge(
                self.mission_id, episode_id=self.episode.episode_id,
                reason="service_identity_changed", knowledge_ids=stale,
            )
            for knowledge_id in stale:
                self._observations.pop(knowledge_id, None)

    def read_for_task(self, task_id: str, knowledge_id: str) -> dict[str, Any]:
        task = self.service.store.get_task(task_id)
        if task is None or task.mission_id != self.mission_id:
            raise ValueError("Task is outside the bound Mission")
        return read_knowledge_tool(
            self.service.store, self.mission_id, "knowledge_read", {"id": knowledge_id},
            sync_currentness=self.service.sync_host_knowledge,
        )


__all__ = ("AppWorldKnowledgeBridge", "make_appworld_knowledge")
