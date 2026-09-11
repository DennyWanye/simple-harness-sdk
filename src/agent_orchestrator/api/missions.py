# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Mission API (ORCH §4.2 ``api/missions.py``): create / get / cancel / events.

``tenant_id`` is a parameter of the API caller — never read from an Agent payload
(D25).  ``create`` validates the charter (§5): non-empty success criteria, a legal
budget and a tool set the deployment actually offers; it is idempotent on
``(tenant_id, idempotency_key)``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import Budget, ContractError, Event, Mission
from ..orchestrator.commit_service import CommitService, MissionSpec
from ..runtime.tool_gateway import TOOL_NAMES


class MissionRequestError(ValueError):
    pass


def validate_spec(spec: MissionSpec, *, available_tools: Sequence[str] = TOOL_NAMES) -> None:
    if not spec.goal.strip():
        raise MissionRequestError("goal must not be blank")
    if not spec.success_criteria or any(not c.strip() for c in spec.success_criteria):
        raise MissionRequestError("success_criteria must be a non-empty list of non-blank strings")
    if not spec.tenant_id.strip() or not spec.idempotency_key.strip():
        raise MissionRequestError("tenant_id and idempotency_key are required")
    unknown = set(spec.allowed_tools) - set(available_tools)
    if unknown:
        raise MissionRequestError(
            f"allowed_tools not offered by this deployment: {sorted(unknown)}"
        )
    if not isinstance(spec.budget, Budget):
        raise MissionRequestError("budget must be a Budget")
    if spec.budget.max_attempts is not None and spec.budget.max_attempts < 1:
        raise MissionRequestError("budget.max_attempts must be at least 1")
    for path in spec.workspace_seed:
        if path.startswith("/") or ".." in path.split("/"):
            raise MissionRequestError(f"workspace_seed path escapes the workspace: {path}")


def spec_from_request(
    tenant_id: str, request: Mapping[str, Any], *, default_tools: Sequence[str] = TOOL_NAMES
) -> MissionSpec:
    """The charter a caller's request describes; an omitted tool set is ``default_tools``
    (the deployment's, when an orchestrator parses it — host support 0.9.8)."""

    try:
        return MissionSpec(
            goal=str(request.get("goal", "")),
            success_criteria=tuple(request.get("success_criteria", ())),
            tenant_id=tenant_id,
            idempotency_key=str(request.get("idempotency_key", "")),
            stop_conditions=tuple(
                request.get("stop_conditions", ("verification_passed", "budget_exhausted"))
            ),
            allowed_tools=tuple(request.get("allowed_tools", default_tools)),
            risk_level=str(request.get("risk_level", "sandbox")),
            budget=Budget.from_json(request.get("budget", {})),
            task_kind=str(request.get("task_kind", "code")),
            workspace_seed=dict(request.get("workspace_seed", {})),
        )
    except (ContractError, TypeError, ValueError) as error:
        raise MissionRequestError(str(error)) from error


class MissionApi:
    def __init__(self, commit: CommitService, *, orchestrator: Any = None) -> None:
        """With ``orchestrator`` (host support 0.9.8, plan review P1-1) ``create`` goes
        through :meth:`Orchestrator.create_mission` — the door that knows the deployment,
        the action criteria, the provider kind and the policy binding."""

        self._commit = commit
        self._orchestrator = orchestrator

    def create(self, *, tenant_id: str, request: Mapping[str, Any]) -> tuple[Mission, bool]:
        if self._orchestrator is not None:
            created: tuple[Mission, bool] = self._orchestrator.create_mission(
                tenant_id=tenant_id, request=request
            )
            return created
        spec = spec_from_request(tenant_id, request)
        validate_spec(spec)
        return self._commit.create_mission(spec)

    def get(self, mission_id: str) -> dict[str, Any]:
        return self._commit.store.snapshot(mission_id)

    def cancel(self, mission_id: str) -> Mission:
        return self._commit.cancel_mission(mission_id)

    def events(self, mission_id: str, *, after_seq: int = 0) -> list[Event]:
        return self._commit.store.list_events(mission_id, after_seq=after_seq)


__all__ = ("MissionApi", "MissionRequestError", "spec_from_request", "validate_spec")
