# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The H4 event-to-decision boundary used by hierarchical dispatch.

The legacy event loop can call :func:`RepairEventAdapter.dispatch` without
knowing anything about model hints or graph traversal.  This adapter has no
side effects: the caller remains responsible for handing an admitted action to
the existing compiler/CommitService.  Its output is therefore suitable for an
audit event and safe to replay after a process restart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...contracts.models import ContractError
from .repair_decision import (
    HumanRequestV1,
    ImpactAnalysis,
    RepairAction,
    RepairActionType,
    RepairCause,
    RepairDecision,
    RepairDecisionStatus,
    RepairRequestV1,
    RepairTriggerSource,
    analyze_impact,
    validate_repair_decision,
)

EVENT_TRIGGER_MAP: dict[str, RepairTriggerSource] = {
    "worker_reject": RepairTriggerSource.WORKER_REJECT,
    "workerrejected": RepairTriggerSource.WORKER_REJECT,
    "verifier_reject": RepairTriggerSource.VERIFIER_ACCEPTANCE_REJECT,
    "acceptance_reject": RepairTriggerSource.VERIFIER_ACCEPTANCE_REJECT,
    "verifieracceptancerejected": RepairTriggerSource.VERIFIER_ACCEPTANCE_REJECT,
    "evidence_invalidation": RepairTriggerSource.EVIDENCE_INVALIDATION,
    "evidenceinvalidated": RepairTriggerSource.EVIDENCE_INVALIDATION,
    "runtime_unavailable": RepairTriggerSource.RUNTIME_UNAVAILABLE,
    "runtimeunavailable": RepairTriggerSource.RUNTIME_UNAVAILABLE,
    "requirements_update": RepairTriggerSource.REQUIREMENTS_UPDATE,
    "requirementsupdated": RepairTriggerSource.REQUIREMENTS_UPDATE,
}


def _event_value(event: Mapping[str, Any] | object, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _normal_event_type(value: object) -> str:
    return str(value or "").strip().replace("-", "_").replace(" ", "_").lower()


def _refs_from_event(event: Mapping[str, Any] | object) -> tuple[object, ...]:
    refs = _event_value(event, "trigger_refs")
    if refs is None:
        payload = _event_value(event, "payload", {})
        refs = payload.get("trigger_refs", ()) if isinstance(payload, Mapping) else ()
    if refs is None:
        refs = _event_value(event, "task_id") or _event_value(event, "attempt_id")
    if isinstance(refs, (str, bytes)):
        return (refs,)
    return tuple(refs or ())


@dataclass(frozen=True, slots=True)
class RepairDispatchResult:
    request: RepairRequestV1
    impact: ImpactAnalysis
    decision: RepairDecision
    status: RepairDecisionStatus

    def audit_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request.request_id,
            "idempotency_key": self.request.idempotency_key,
            "trigger_source": str(self.request.trigger_source),
            "impact": self.impact.to_json(),
            "decision": self.decision.to_json(),
            "status": str(self.status),
        }


class RepairEventAdapter:
    """Normalize durable event rows and execute the pure H4 decision boundary."""

    @staticmethod
    def request_from_event(
        event: Mapping[str, Any] | object,
        *,
        mission_id: str | None = None,
        plan_revision: int = 0,
        diagnosis: RepairCause | str = RepairCause.UNKNOWN,
        affected_hints: Sequence[str] = (),
    ) -> RepairRequestV1:
        explicit = _event_value(event, "trigger_source")
        source = (
            RepairTriggerSource(explicit)
            if explicit is not None
            else EVENT_TRIGGER_MAP.get(_normal_event_type(_event_value(event, "type")))
        )
        if source is None:
            raise ContractError("event does not identify one of the five H4 repair triggers")
        payload = _event_value(event, "payload", {})
        payload = payload if isinstance(payload, Mapping) else {}
        context = dict(payload.get("context", {}))
        context.setdefault("event_type", str(_event_value(event, "type", "")))
        return RepairRequestV1.from_trigger(
            source,
            trigger_refs=_refs_from_event(event),
            mission_id=mission_id
            or str(_event_value(event, "mission_id", payload.get("mission_id", "mission"))),
            plan_revision=plan_revision,
            diagnosis=diagnosis,
            affected_hints=tuple(affected_hints),
            context=context,
            repair_round=int(payload.get("repair_round", 0)),
        )

    @staticmethod
    def dispatch(
        event: Mapping[str, Any] | object,
        *,
        actions: Sequence[RepairAction | Mapping[str, Any]],
        all_items: Iterable[str] = (),
        reverse_data: Mapping[str, Iterable[str]] | None = None,
        reverse_support: Mapping[str, Iterable[str]] | None = None,
        method_membership: Mapping[str, Iterable[str]] | None = None,
        demand_refs: Mapping[str, Iterable[str]] | None = None,
        acceptance_refs: Mapping[str, Iterable[str]] | None = None,
        operation_states: Mapping[str, str] | None = None,
        stale_items: Iterable[str] = (),
        new_work: Iterable[str] = (),
        mission_id: str | None = None,
        plan_revision: int = 0,
        diagnosis: RepairCause | str = RepairCause.UNKNOWN,
        affected_hints: Sequence[str] = (),
        human_request: HumanRequestV1 | None = None,
        human_authorized: bool = False,
    ) -> RepairDispatchResult:
        request = RepairEventAdapter.request_from_event(
            event,
            mission_id=mission_id,
            plan_revision=plan_revision,
            diagnosis=diagnosis,
            affected_hints=affected_hints,
        )
        impact = analyze_impact(
            request,
            all_items=all_items,
            reverse_data=reverse_data,
            reverse_support=reverse_support,
            method_membership=method_membership,
            demand_refs=demand_refs,
            acceptance_refs=acceptance_refs,
            operation_states=operation_states,
            stale_items=stale_items,
            new_work=new_work,
        )
        normalized_actions = tuple(
            action if isinstance(action, RepairAction) else RepairAction.from_json(action)
            for action in actions
        )
        decision = RepairDecision(
            request=request,
            actions=normalized_actions,
            impact=impact,
            human_request=human_request,
        )
        status = validate_repair_decision(decision, human_authorized=human_authorized)
        return RepairDispatchResult(request, impact, decision, status)

    @staticmethod
    def retry_same_method(event: Mapping[str, Any] | object, **kwargs: Any) -> RepairDispatchResult:
        return RepairEventAdapter.dispatch(
            event, actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),), **kwargs
        )

    @staticmethod
    def refine_deeper(event: Mapping[str, Any] | object, **kwargs: Any) -> RepairDispatchResult:
        return RepairEventAdapter.dispatch(
            event, actions=(RepairAction(RepairActionType.REFINE_DEEPER),), **kwargs
        )

    @staticmethod
    def replace_method(event: Mapping[str, Any] | object, **kwargs: Any) -> RepairDispatchResult:
        return RepairEventAdapter.dispatch(
            event, actions=(RepairAction(RepairActionType.REPLACE_METHOD),), **kwargs
        )

    @staticmethod
    def declare_runtime_blocked(
        event: Mapping[str, Any] | object, **kwargs: Any
    ) -> RepairDispatchResult:
        return RepairEventAdapter.dispatch(
            event,
            actions=(RepairAction(RepairActionType.DECLARE_RUNTIME_BLOCKED),),
            **kwargs,
        )


__all__ = ["EVENT_TRIGGER_MAP", "RepairDispatchResult", "RepairEventAdapter"]
