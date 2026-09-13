# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Bounded, informational Worker projection of the deployed action candidate contract.

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

ACTION_CANDIDATE_CONTEXT_VERSION = "action-candidate-context-v1"


def worker_action_contract(
    *,
    mission_criteria: Sequence[str],
    task_criteria: Sequence[str],
    task_outputs: Sequence[str],
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
) -> dict[str, Any] | None:
    """Only operations explicitly in this Task, Mission and deployed connector.

    A descriptor's ``required_params`` describes execution *after* canonical Artifact
    binding. Publishing is the one connector whose model-facing parameter is different.
    Other descriptors are projected as declared; no parameter types are guessed.
    """

    action_outputs = sorted({path for path in task_outputs
                             if path.startswith("actions/") and path.endswith(".json")})
    if not action_outputs:
        return None
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
        "output_files": action_outputs,
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
            "an artifact_path from its own Result; the system binds artifact identity after "
            "acceptance. The original scope, deployment, rule_check and approval gates apply."
        ),
    }


__all__ = ("ACTION_CANDIDATE_CONTEXT_VERSION", "worker_action_contract")
