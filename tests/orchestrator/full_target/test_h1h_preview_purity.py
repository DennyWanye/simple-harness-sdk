"""P05: production collector freezes the inputs; real preview remains pure.

The capture stop is intentionally a BaseException so the collector cannot translate
it into a planning refusal/retry; all writes before that stop establish the legal
request/decision fixture and are outside the purity measurement.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from test_h1i_production_entry import (
    _config,
    _open_planner_round,
    _refine_reply,
    _seed_new_protocol,
)

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.plan_preview import CandidatePreview
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


class _PreviewInputsCaptured(BaseException):
    pass


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts
    ):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_p05_same_frozen_production_preview_is_deterministic_and_side_effect_free(
    tmp_path: Path,
) -> None:
    async def case() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="p05-preview-purity"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="p05-preview-grant", request_id=opener.intent_id)

            captured: dict[str, Any] = {}
            original_preview = dispatch.preview_plan_proposal

            def capture(proposal: Any, *, inputs: Any) -> Any:
                captured["proposal"] = proposal
                captured["inputs"] = inputs
                raise _PreviewInputsCaptured()

            dispatch.preview_plan_proposal = capture  # type: ignore[method-assign]
            try:
                await loop._collect_plan_decision(
                    opener,
                    object(),
                    mission,
                    _refine_reply(opener.config["planning_package"]),
                    dispatch,
                )
            except _PreviewInputsCaptured:
                pass
            finally:
                dispatch.preview_plan_proposal = original_preview  # type: ignore[method-assign]

            assert set(captured) == {"proposal", "inputs"}
            db_before = loop.store.connection.total_changes
            repo_before = _tree_hash(tmp_path / "repo")
            provider_before = provider.calls
            gateway_before = len(loop.assembled.gateway.calls)

            first = dispatch.preview_plan_proposal(captured["proposal"], inputs=captured["inputs"])
            second = dispatch.preview_plan_proposal(captured["proposal"], inputs=captured["inputs"])

            assert isinstance(first, CandidatePreview)
            assert isinstance(second, CandidatePreview)
            assert first.compilation_hash == second.compilation_hash
            assert first.source_snapshot_hash == second.source_snapshot_hash
            assert loop.store.connection.total_changes == db_before
            assert _tree_hash(tmp_path / "repo") == repo_before
            assert provider.calls == provider_before == 0
            assert len(loop.assembled.gateway.calls) == gateway_before == 0

    asyncio.run(case())
