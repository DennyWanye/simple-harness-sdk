# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Bounded planning/execution projection of the deployed action candidate contract.

The actual authority stays with check_candidate, bind_artifact_params and approval.
Never serialize connector objects or their machine-local configuration into context.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..governance.policies import DeploymentPolicy, action_decision
from ..orchestrator.action_commits import (
    BOUND_ARTIFACT_FIELDS,
    CANDIDATE_FIELDS,
    parse_action_criterion,
)
from .connectors_publish import FilePublishConnector

ACTION_CANDIDATE_CONTEXT_VERSION = "action-candidate-context-v2"


def _action_contract(
    *,
    mission_criteria: Sequence[str],
    task_criteria: Sequence[str],
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
) -> dict[str, Any] | None:
    """Only operations explicitly in this Task, Mission and deployed connector.

    A descriptor's ``required_params`` describes execution *after* canonical Artifact
    binding. Publishing is the one connector whose model-facing parameter is different.
    Other descriptors are projected as declared; no parameter types are guessed.
    """

    charter = {parsed for criterion in mission_criteria
               if (parsed := parse_action_criterion(criterion)) is not None}
    task_scope = {parsed for criterion in task_criteria
                  if (parsed := parse_action_criterion(criterion)) is not None}
    # A Task may declare its action solely by its actions/*.json output, with the
    # action criterion remaining at Mission level. Never infer an operation outside
    # the Mission charter; Task-level action criteria narrow it when present.
    relevant = sorted(charter.intersection(task_scope) if task_scope else charter)
    operations: list[dict[str, Any]] = []
    for name, operation, target in relevant:
        connector = connectors.get(name)
        spec = (getattr(connector, "operations", {}) or {}).get(operation)
        decision = action_decision(deployment, connector, operation)
        if spec is None or decision.refused is not None:
            continue
        required = tuple(spec.required_params)
        is_publish = (name == "file_publish" and operation == "publish"
                      and isinstance(connector, FilePublishConnector))
        if is_publish:
            if not {"artifact_path", "content_hash", "storage_uri"}.issubset(required):
                continue  # declaration changed; do not invent a model-facing schema
            model_required = [field for field in required if field not in BOUND_ARTIFACT_FIELDS]
        elif BOUND_ARTIFACT_FIELDS.intersection(required):
            continue  # cannot describe an unrecognised system-bound operation
        else:
            model_required = list(required)
        operations.append({
            "connector": name,
            "operation": operation,
            "target": target,
            "level": decision.level,
            "required_approvals": decision.required_approvals,
            "required_model_params": model_required,
            **({"system_bound_artifact_fields": sorted(BOUND_ARTIFACT_FIELDS)}
               if is_publish else {}),
        })
    if not operations:
        return None
    # The normal ContextPackage/request budget bounds this projection. Do not
    # introduce a smaller, unrelated limit on otherwise valid action contracts.
    return {
        "version": ACTION_CANDIDATE_CONTEXT_VERSION,
        "required_fields": list(CANDIDATE_FIELDS),
        "additional_fields": False,
        "field_types": {
            "connector": "non-empty string", "operation": "non-empty string",
            "target": "non-empty string", "params": "object", "reason": "non-empty string",
        },
        "operations": operations,
        "notice": (
            "Write one JSON object per actions/*.json file. The listed operations describe "
            "candidate shape, not permission or approval. A publish candidate names only "
            "an artifact_path identifying the user's intended content file from its own "
            "Result, not the candidate JSON or a newly invented publication description. "
            "The target is the destination after publication, not an extra workspace "
            "output requirement. Preserve the intended source file in the Task goal. "
            "The system binds artifact identity after "
            "acceptance. The original scope, deployment, rule_check and approval gates apply."
        ),
    }


def worker_action_contract(
    *,
    mission_criteria: Sequence[str],
    task_criteria: Sequence[str],
    task_outputs: Sequence[str],
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
) -> dict[str, Any] | None:
    outputs = sorted({path for path in task_outputs
                      if path.startswith("actions/") and path.endswith(".json")})
    if not outputs:
        return None
    contract = _action_contract(
        mission_criteria=mission_criteria, task_criteria=task_criteria,
        connectors=connectors, deployment=deployment,
    )
    return None if contract is None else {**contract, "output_files": outputs}


def planner_action_contract(
    *,
    mission_criteria: Sequence[str],
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
) -> dict[str, Any] | None:
    contract = _action_contract(
        mission_criteria=mission_criteria, task_criteria=(),
        connectors=connectors, deployment=deployment,
    )
    if contract is None:
        return None
    return {
        **contract,
        "candidate_output_pattern": "actions/*.json",
        "planning_notice": (
            "Declare the content artifact and an actions/*.json candidate in the producing "
            "Task outputs. Preserve the Mission's requested content and source file; an "
            "action criterion's target is an external destination, not a file to generate. "
            "L2/L3 action approval is requested separately after Result acceptance. "
            "Do not add human_review merely to implement that action approval; retain "
            "human_review when the user separately requires review of the content."
        ),
    }


__all__ = (
    "ACTION_CANDIDATE_CONTEXT_VERSION", "worker_action_contract", "planner_action_contract",
)
