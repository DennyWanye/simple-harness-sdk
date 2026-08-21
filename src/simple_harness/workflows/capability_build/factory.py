# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public durable-task specialization for capability construction."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.workflow.contracts import (
    CapabilityBuildHostServices,
    ChannelSpec,
    JsonType,
    ReducerKind,
    StatePatch,
    WorkflowContext,
    WorkflowState,
    validate_json_value,
)
from simple_harness.workflow.definition import (
    END_NODE,
    Edge,
    NodeDefinition,
    WorkflowDefinition,
    WorkflowDefinitionRegistration,
)
from simple_harness.workflows._registration import build_registration

from ._constants import (
    DEFAULT_FIX_BUDGET,
    DEFAULT_PROPOSAL_BUDGET,
    WORKFLOW_NAME,
    WORKFLOW_PROFILE_KEY,
    WORKFLOW_VERSION,
)
from .ports import (
    CapabilityActivatePort,
    CapabilityBuildAuthorizationPort,
    CapabilitySearchPort,
    CapabilitySourcePolicyPort,
    IsolatedBuildPort,
    PackageStorePort,
)

START_SCHEMA_REF = "sdk://workflow/capability-build/v1/start"
START_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "request": {"type": "string", "minLength": 1, "maxLength": 65536},
        "search_miss_receipt": {"type": "string", "minLength": 1, "maxLength": 4096},
        "proposal_budget": {
            "type": "integer",
            "minimum": 1,
            "maximum": DEFAULT_PROPOSAL_BUDGET,
        },
        "fix_budget": {
            "type": "integer",
            "minimum": 0,
            "maximum": DEFAULT_FIX_BUDGET,
        },
    },
    "required": ["request", "search_miss_receipt"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class CapabilityBuildAdmission:
    """Validated, fingerprinted input for one durable specialization run."""

    run_id: str
    request: str
    search_miss_receipt: str
    proposal_budget: int = DEFAULT_PROPOSAL_BUDGET
    fix_budget: int = DEFAULT_FIX_BUDGET

    def __post_init__(self) -> None:
        for name in ("run_id", "request", "search_miss_receipt"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if (
            isinstance(self.proposal_budget, bool)
            or not 1 <= self.proposal_budget <= DEFAULT_PROPOSAL_BUDGET
        ):
            raise ValueError("proposal budget is outside capability-build admission")
        if isinstance(self.fix_budget, bool) or not 0 <= self.fix_budget <= DEFAULT_FIX_BUDGET:
            raise ValueError("fix budget is outside capability-build admission")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "profile_key": WORKFLOW_PROFILE_KEY,
            "workflow_name": WORKFLOW_NAME,
            "workflow_version": WORKFLOW_VERSION,
            "run_id": self.run_id,
            "request": self.request,
            "search_miss_receipt": self.search_miss_receipt,
            "proposal_budget": self.proposal_budget,
            "fix_budget": self.fix_budget,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityBuildExecutionState:
    """JSON-round-trippable terminal state used for close/reopen recovery."""

    admission_fingerprint: str
    activation_key: str
    phase: str
    active: bool
    terminal_status: str
    package_ref: str
    activation_receipt: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        for name in (
            "admission_fingerprint",
            "activation_key",
            "phase",
            "terminal_status",
            "package_ref",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        receipt = copy.deepcopy(dict(self.activation_receipt))
        validate_json_value(receipt, path="$.activation_receipt")
        object.__setattr__(self, "activation_receipt", MappingProxyType(receipt))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "admission_fingerprint": self.admission_fingerprint,
            "activation_key": self.activation_key,
            "phase": self.phase,
            "active": self.active,
            "terminal_status": self.terminal_status,
            "package_ref": self.package_ref,
            "activation_receipt": copy.deepcopy(dict(self.activation_receipt)),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> CapabilityBuildExecutionState:
        receipt = value.get("activation_receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("activation_receipt must be an object")
        return cls(
            admission_fingerprint=str(value.get("admission_fingerprint", "")),
            activation_key=str(value.get("activation_key", "")),
            phase=str(value.get("phase", "")),
            active=value.get("active") is True,
            terminal_status=str(value.get("terminal_status", "")),
            package_ref=str(value.get("package_ref", "")),
            activation_receipt=cast(Mapping[str, JsonValue], receipt),
        )


def _json_object(value: object, *, boundary: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{boundary} must return a JSON object")
    detached = copy.deepcopy(dict(value))
    validate_json_value(detached, path=f"$.{boundary}")
    return cast(dict[str, JsonValue], detached)


def _operation_key(admission_fingerprint: str, stage: str) -> str:
    return hashlib.sha256(f"{admission_fingerprint}|{stage}".encode()).hexdigest()


async def run_capability_build_specialization(
    *,
    admission: CapabilityBuildAdmission,
    services: CapabilityBuildHostServices,
    prior_state: CapabilityBuildExecutionState | None = None,
) -> CapabilityBuildExecutionState:
    """Run the bounded physical path and recover terminal activation exactly once."""

    if not isinstance(admission, CapabilityBuildAdmission):
        raise TypeError("admission must be a CapabilityBuildAdmission")
    if not isinstance(services, CapabilityBuildHostServices):
        raise TypeError("services must be CapabilityBuildHostServices")
    fingerprint = admission.fingerprint
    if prior_state is not None:
        if not isinstance(prior_state, CapabilityBuildExecutionState):
            raise TypeError("prior_state must be CapabilityBuildExecutionState")
        if prior_state.admission_fingerprint != fingerprint:
            raise ValueError("prior state belongs to a different admission")
        if (
            prior_state.active
            and prior_state.phase == "activated"
            and prior_state.terminal_status == "completed"
        ):
            return prior_state
        raise ValueError("non-terminal capability-build state cannot be recovered")

    admission_payload = admission.to_json()
    authorization = _json_object(
        await cast(CapabilityBuildAuthorizationPort, services.authorization).authorize_build(
            operation_key=_operation_key(fingerprint, "authorization"),
            admission=admission_payload,
        ),
        boundary="authorization",
    )
    if authorization.get("allowed") is not True:
        raise PermissionError("capability-build admission was not authorized")
    search = _json_object(
        await cast(CapabilitySearchPort, services.search).search(
            query=admission.request,
            operation_key=_operation_key(fingerprint, "search"),
            admission=admission_payload,
        ),
        boundary="search",
    )
    source = search.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("capability search omitted source")
    source_policy = _json_object(
        await cast(CapabilitySourcePolicyPort, services.source_policy).authorize_source(
            source=source,
            operation_key=_operation_key(fingerprint, "source_policy"),
            admission=admission_payload,
        ),
        boundary="source_policy",
    )
    if source_policy.get("allowed") is not True:
        raise PermissionError("capability source policy rejected the source")
    built = _json_object(
        await cast(IsolatedBuildPort, services.isolated_build).build(
            candidate=search.get("candidate"),
            source_policy=source_policy,
            operation_key=_operation_key(fingerprint, "isolated_build"),
            admission=admission_payload,
        ),
        boundary="isolated_build",
    )
    if "package" not in built:
        raise ValueError("isolated build omitted package")
    stored = _json_object(
        await cast(PackageStorePort, services.package_store).store(
            package=built["package"],
            operation_key=_operation_key(fingerprint, "package_store"),
            admission=admission_payload,
        ),
        boundary="package_store",
    )
    package_ref = stored.get("package_ref")
    if not isinstance(package_ref, str) or not package_ref:
        raise ValueError("package store omitted package_ref")
    activation_key = _operation_key(fingerprint, "activate")
    activated = _json_object(
        await cast(CapabilityActivatePort, services.activate).activate(
            package_ref=package_ref,
            activation_key=activation_key,
            operation_key=activation_key,
            admission=admission_payload,
        ),
        boundary="activate",
    )
    if activated.get("active") is not True:
        raise RuntimeError("capability activation did not reach active state")
    return CapabilityBuildExecutionState(
        admission_fingerprint=fingerprint,
        activation_key=activation_key,
        phase="activated",
        active=True,
        terminal_status="completed",
        package_ref=package_ref,
        activation_receipt=activated,
    )


def create_initial_state(
    *,
    run_id: str,
    request: str,
    search_miss_receipt: str,
    proposal_budget: int = DEFAULT_PROPOSAL_BUDGET,
    fix_budget: int = DEFAULT_FIX_BUDGET,
    thread_id: str | None = None,
    session_id: str = "",
) -> WorkflowState:
    """Build the durable native state for the capability specialization."""

    admission = CapabilityBuildAdmission(
        run_id=run_id,
        request=request,
        search_miss_receipt=search_miss_receipt,
        proposal_budget=proposal_budget,
        fix_budget=fix_budget,
    )
    return {
        "schema_version": 1,
        "workflow_name": WORKFLOW_NAME,
        "workflow_version": WORKFLOW_VERSION,
        "thread_id": thread_id or run_id,
        "run_id": run_id,
        "session_id": session_id,
        "active_nodes": [],
        "active_step_id": None,
        "status": "pending",
        "values": {
            "request": admission.request,
            "search_miss_receipt": admission.search_miss_receipt,
            "proposal_budget": admission.proposal_budget,
            "fix_budget": admission.fix_budget,
        },
        "blob_refs": [],
        "artifact_refs": [],
        "receipt_refs": [],
        "loop_counters": {},
        "budgets": {},
        "errors": [],
    }


def _admission_from_state(state: WorkflowState) -> CapabilityBuildAdmission:
    values = dict(state.get("values") or {})
    return CapabilityBuildAdmission(
        run_id=str(state.get("run_id", "")),
        request=str(values.get("request", "")),
        search_miss_receipt=str(values.get("search_miss_receipt", "")),
        proposal_budget=cast(int, values.get("proposal_budget", DEFAULT_PROPOSAL_BUDGET)),
        fix_budget=cast(int, values.get("fix_budget", DEFAULT_FIX_BUDGET)),
    )


def _stage_patch(
    state: WorkflowState,
    admission: CapabilityBuildAdmission,
    stage: str,
    result: Mapping[str, JsonValue],
) -> StatePatch:
    values = dict(state.get("values") or {})
    progress = dict(values.get("capability_build_progress") or {})
    progress[stage] = {
        "admission_fingerprint": admission.fingerprint,
        "operation_key": _operation_key(admission.fingerprint, stage),
    }
    return StatePatch(
        {
            "values": {
                **values,
                f"{stage}_result": copy.deepcopy(dict(result)),
                "capability_build_progress": progress,
            }
        }
    )


def _required_result(state: WorkflowState, stage: str) -> dict[str, JsonValue]:
    result = dict(state.get("values") or {}).get(f"{stage}_result")
    if not isinstance(result, Mapping):
        raise ValueError(f"capability-build {stage} result is unavailable")
    return _json_object(result, boundary=stage)


async def _authorization_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    admission = _admission_from_state(state)
    result = _json_object(
        await cast(CapabilityBuildAuthorizationPort, context.port("authorization")).authorize_build(
            operation_key=_operation_key(admission.fingerprint, "authorization"),
            admission=admission.to_json(),
        ),
        boundary="authorization",
    )
    if result.get("allowed") is not True:
        raise PermissionError("capability-build admission was not authorized")
    return _stage_patch(state, admission, "authorization", result)


async def _search_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    admission = _admission_from_state(state)
    result = _json_object(
        await cast(CapabilitySearchPort, context.port("capability_search")).search(
            query=admission.request,
            operation_key=_operation_key(admission.fingerprint, "search"),
            admission=admission.to_json(),
        ),
        boundary="search",
    )
    source = result.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("capability search omitted source")
    return _stage_patch(state, admission, "search", result)


async def _source_policy_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    admission = _admission_from_state(state)
    search = _required_result(state, "search")
    result = _json_object(
        await cast(CapabilitySourcePolicyPort, context.port("source_policy")).authorize_source(
            source=cast(str, search["source"]),
            operation_key=_operation_key(admission.fingerprint, "source_policy"),
            admission=admission.to_json(),
        ),
        boundary="source_policy",
    )
    if result.get("allowed") is not True:
        raise PermissionError("capability source policy rejected the source")
    return _stage_patch(state, admission, "source_policy", result)


async def _isolated_build_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    admission = _admission_from_state(state)
    search = _required_result(state, "search")
    source_policy = _required_result(state, "source_policy")
    result = _json_object(
        await cast(IsolatedBuildPort, context.port("isolated_build")).build(
            candidate=search.get("candidate"),
            source_policy=source_policy,
            operation_key=_operation_key(admission.fingerprint, "isolated_build"),
            admission=admission.to_json(),
        ),
        boundary="isolated_build",
    )
    if "package" not in result:
        raise ValueError("isolated build omitted package")
    return _stage_patch(state, admission, "isolated_build", result)


async def _package_store_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    admission = _admission_from_state(state)
    built = _required_result(state, "isolated_build")
    result = _json_object(
        await cast(PackageStorePort, context.port("package_store")).store(
            package=built["package"],
            operation_key=_operation_key(admission.fingerprint, "package_store"),
            admission=admission.to_json(),
        ),
        boundary="package_store",
    )
    package_ref = result.get("package_ref")
    if not isinstance(package_ref, str) or not package_ref:
        raise ValueError("package store omitted package_ref")
    return _stage_patch(state, admission, "package_store", result)


async def _activate_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    admission = _admission_from_state(state)
    stored = _required_result(state, "package_store")
    package_ref = cast(str, stored["package_ref"])
    activation_key = _operation_key(admission.fingerprint, "activate")
    activated = _json_object(
        await cast(CapabilityActivatePort, context.port("activate")).activate(
            package_ref=package_ref,
            activation_key=activation_key,
            operation_key=activation_key,
            admission=admission.to_json(),
        ),
        boundary="activate",
    )
    if activated.get("active") is not True:
        raise RuntimeError("capability activation did not reach active state")
    completed = CapabilityBuildExecutionState(
        admission_fingerprint=admission.fingerprint,
        activation_key=activation_key,
        phase="activated",
        active=True,
        terminal_status="completed",
        package_ref=package_ref,
        activation_receipt=activated,
    )
    values = dict(state.get("values") or {})
    progress = dict(values.get("capability_build_progress") or {})
    progress["activate"] = {
        "admission_fingerprint": admission.fingerprint,
        "operation_key": activation_key,
    }
    return StatePatch(
        {
            "values": {
                **values,
                "capability_build": completed.to_json(),
                "capability_build_progress": progress,
                "active": completed.active,
                "terminal_status": "success",
            }
        }
    )


def build_capability_build_definition() -> WorkflowDefinition:
    """Return the SDK-owned durable graph for the bounded specialization."""

    return WorkflowDefinition(
        name=WORKFLOW_NAME,
        version=WORKFLOW_VERSION,
        state_schema_version=1,
        entry_node="authorization",
        nodes=(
            NodeDefinition("authorization", _authorization_handler),
            NodeDefinition("search", _search_handler),
            NodeDefinition("source_policy", _source_policy_handler),
            NodeDefinition("isolated_build", _isolated_build_handler),
            NodeDefinition("package_store", _package_store_handler),
            NodeDefinition("activate", _activate_handler),
        ),
        channels={
            "values": ChannelSpec(
                value_type=JsonType.OBJECT,
                reducer=ReducerKind.SINGLE_WRITER,
                allowed_writers=frozenset(
                    {
                        "authorization",
                        "search",
                        "source_policy",
                        "isolated_build",
                        "package_store",
                        "activate",
                    }
                ),
            )
        },
        recursion_limit=16,
        max_supersteps=12,
        edges=(
            Edge("authorization", "search"),
            Edge("search", "source_policy"),
            Edge("source_policy", "isolated_build"),
            Edge("isolated_build", "package_store"),
            Edge("package_store", "activate"),
            Edge("activate", END_NODE),
        ),
        prompt_manifest={"profile": WORKFLOW_PROFILE_KEY},
        policy_manifest={
            "implementation": "capability-build-specialization-v1",
            "operation_identity": "admission-fingerprint-stage-v1",
            "activation": "host-idempotent-stable-key-v1",
            "required_admission": "search-miss-receipt-v1",
        },
    )


def build_capability_build_registration(
    *, generation: int, transaction_owner: object
) -> WorkflowDefinitionRegistration:
    return build_registration(
        profile_key=WORKFLOW_PROFILE_KEY,
        description="Build, validate, store, and activate a capability package.",
        use_when="Capability search produced a durable miss receipt.",
        avoid_when="An existing capability already satisfies the request.",
        schema_ref=START_SCHEMA_REF,
        schema=START_SCHEMA,
        generation=generation,
        definition=build_capability_build_definition(),
        transaction_owner=transaction_owner,
    )


__all__ = (
    "CapabilityBuildAdmission",
    "CapabilityBuildExecutionState",
    "START_SCHEMA",
    "START_SCHEMA_REF",
    "build_capability_build_definition",
    "build_capability_build_registration",
    "create_initial_state",
    "run_capability_build_specialization",
)
