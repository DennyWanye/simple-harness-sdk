# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Bounded planning/execution projection of the deployed action candidate contract.

The actual authority stays with check_candidate, bind_artifact_params and approval.
Never serialize connector objects or their machine-local configuration into context.
"""

from __future__ import annotations

import json
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

#: The output port through which a hierarchical primitive delivers its operation
#: candidate.  A deployment that declares it (the desktop's task types do when the
#: Mission carries an ``action:`` criterion) gets the candidate contract into the
#: Worker's package, with the file under ``actions/`` where the operation workspace
#: reads candidates.  Without it the Worker was never told the candidate shape and
#: invented one outside ``actions/`` (NEXT-TG-1.0 2A upstream run, 2026-09-27).
OPERATION_CANDIDATE_PORT = "action_candidate"
OPERATION_CANDIDATE_FILE = f"actions/{OPERATION_CANDIDATE_PORT}.json"


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


def declared_action_outputs(store: Any, mission_id: str, task: Any) -> tuple[str, ...]:
    """The Task's declared files plus, for a hierarchical primitive with the operation
    candidate port, its candidate file.

    The one answer the Worker's candidate contract, the result's rule check and the
    accept transaction all use — three separate readings of ``task.outputs`` made the
    candidate the contract asked for "undeclared" twice over (2A upstream runs).
    """
    from ..storage.htn_store import HtnStore

    outputs = tuple(task.outputs)
    if any(path.startswith("actions/") and path.endswith(".json") for path in outputs):
        return outputs
    binding = HtnStore(store).task_semantics_of(mission_id, task.id)
    if binding is not None and any(port.port_key == OPERATION_CANDIDATE_PORT
                                   for port in binding.output_ports):
        return (*outputs, OPERATION_CANDIDATE_FILE)
    return outputs


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
    if contract is None:
        return None
    return {
        **contract,
        "output_files": outputs,
        # 2A upstream run: a later step named the file an earlier step produced, did
        # not list it among its own artifacts, and its candidate was refused
        # (artifact_not_in_result); listing the received file resolved it.
        "artifact_path_rule": (
            "artifact_path must name a file in this step's own Result. If the file to "
            "publish is one you received unchanged from your inputs, list that file in "
            "your Result artifacts as well."
        ),
    }


def with_candidate_targets(listed: Sequence[Any], workspace: Any) -> list[Any]:
    """The file an operation candidate names is part of the step's own Result.

    2A upstream run: a Worker that publishes a file it received unchanged kept leaving
    it off its artifact list (its template asks for the files it *wrote*), even with
    ``artifact_path_rule`` in its contract, and the candidate was refused as
    ``artifact_not_in_result``.  So the system lists it — only a real file inside this
    Attempt's workspace, never another candidate.  Identity is still bound later from
    the accepted Result by ``bind_artifact_params``; nothing here trusts the model.
    """

    from ..artifacts.workspace import WorkspaceError

    out = list(listed)
    for path in [item for item in listed if isinstance(item, str)]:
        if not (path.startswith("actions/") and path.endswith(".json")):
            continue
        try:
            candidate = json.loads(workspace.read_text(path))
        except (WorkspaceError, OSError, ValueError):
            continue
        params = candidate.get("params") if isinstance(candidate, Mapping) else None
        target = params.get("artifact_path") if isinstance(params, Mapping) else None
        if not isinstance(target, str) or not target.strip():
            continue
        target = target.strip()
        if target in out or target.startswith("actions/"):
            continue
        try:
            if not workspace.resolve(target).is_file():
                continue
        except (WorkspaceError, OSError):
            continue
        out.append(target)
    return out


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
    "ACTION_CANDIDATE_CONTEXT_VERSION", "OPERATION_CANDIDATE_FILE", "OPERATION_CANDIDATE_PORT",
    "declared_action_outputs",
    "worker_action_contract", "planner_action_contract",
)
