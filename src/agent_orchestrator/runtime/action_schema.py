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

    from ..verification.assessments import task_criterion_text

    charter = {parsed for criterion in mission_criteria
               if (parsed := parse_action_criterion(criterion)) is not None}
    # 2026-09-28 真机第四局：分层步骤的要求是编号（c-user-5 = 第 5 条原始要求），直接按
    # 文字解析一条也认不出，每一步都看到了整个任务的全部操作。先还原成原文再收窄。
    task_scope = {parsed for criterion in task_criteria
                  if (parsed := parse_action_criterion(
                      task_criterion_text(criterion, mission_criteria))) is not None}
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
    if len(outputs) == 1 and system_writes_candidate(contract):
        # 2026-09-28 用户决定：用户在确认页已选定"发布哪个文件到哪里"，申请单由系统照此
        # 生成（with_system_candidate），模型只负责把文件内容做对。
        return {
            **contract,
            "output_files": outputs,
            "system_writes_candidate": True,
            "notice": (
                f"The system writes {outputs[0]} for you from the operation the user "
                "confirmed. Do not write any file under actions/. The file published is "
                f"{contract['operations'][0]['target']!r} in your workspace: put the final "
                "content at exactly that path and list it in your Result artifacts."
            ),
        }
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


def system_writes_candidate(contract: Mapping[str, Any] | None) -> bool:
    """Exactly one operation, a file publish whose only model-facing parameter is the file."""

    if contract is None or len(contract.get("operations", ())) != 1:
        return False
    operation = contract["operations"][0]
    return (operation["connector"] == "file_publish" and operation["operation"] == "publish"
            and operation["required_model_params"] == ["artifact_path"])


def with_system_candidate(
    listed: Sequence[Any], workspace: Any, contract: Mapping[str, Any] | None
) -> list[Any]:
    """Write the step's one publish candidate from the confirmed operation.

    2026-09-28 真机第四局：12 次步骤尝试里 8 次浪费在模型写申请单上（漏字段、文件名不对、
    写成非对象、回合用完）。申请单的每个字段都已由用户确认的操作决定：连接器、操作、
    目标，以及要发布的文件——目标的文件名。系统照写，模型写在 actions/ 下的申请单一律
    不收；找不到要发布的文件时不写，照旧由规则检查退回。之后的范围、部署、规则检查、
    产物绑定与审批一个不少。
    """

    from ..artifacts.workspace import WorkspaceError

    if not (contract and contract.get("system_writes_candidate")):
        return list(listed)
    operation = contract["operations"][0]
    candidate_path = contract["output_files"][0]
    target = str(operation["target"])
    # 这一步自己列出的产物优先；它没列（原样转交上游文件）时才看工作区里的同名文件。
    # 提示已告诉模型要发布的文件必须放在目标路径上（审阅 2026-09-28）。
    names = (target, target.rsplit("/", 1)[-1])
    reported = [item.strip() for item in listed if isinstance(item, str)]
    source = None
    for path in sorted(names, key=lambda name: name not in reported):
        try:
            if workspace.resolve(path).is_file():
                source = path
                break
        except (WorkspaceError, OSError):
            continue
    if source is None:
        return list(listed)
    candidate = {
        "connector": operation["connector"],
        "operation": operation["operation"],
        "target": target,
        "params": {"artifact_path": source},
        "reason": f"用户在确认页选定的操作：发布 {source} 到 {target}",
    }
    try:
        workspace.write_text(candidate_path, json.dumps(candidate, ensure_ascii=False, indent=2))
    except (WorkspaceError, OSError):
        return list(listed)  # 写不进去（被做成链接/目录等）：照旧交给规则检查退回
    out = [item for item in listed
           if not (isinstance(item, str) and item.startswith("actions/") and item.endswith(".json"))]
    for path in (source, candidate_path):
        if path not in out:
            out.append(path)
    return out


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
    "system_writes_candidate", "with_system_candidate",
)
