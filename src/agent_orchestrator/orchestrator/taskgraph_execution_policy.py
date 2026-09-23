# SPDX-License-Identifier: Apache-2.0
"""Current execution-policy facts from the original Mission and deployment.

These are the actual four permission inputs and installed resource limits. They
are not planning grants, health probes, Action approvals or dispatch decisions.
The original materialization/provider/tool/Action guards still decide each call.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from simple_harness.contracts import canonical_json
from simple_harness.agents import AgentConfig

from ..contracts.models import sha256_hex
from ..contracts.state_machines import TERMINAL_ATTEMPT, TERMINAL_TASK
from ..governance.policies import DeploymentPolicy, effective_tools
from ..governance.promotion import params_hash
from ..graph.execution_contracts import CompleteRead
from ..runtime.planning_operations import SourceUnavailable
from ..runtime.role_templates import SYNTHESIZER, role_for_task
from ..storage.htn_store import HtnStore
from ..storage.store import DispatchIntent, Store, StoreConflict, StoreError
from .occurrence_tasks import read_only_leaf
from .taskgraph_execution_sources import _document
from .taskgraph_plan_sources import ImportedExecutionSource

RuntimeImportReader = Callable[[Store, str], CompleteRead[ImportedExecutionSource]]


class TaskGraphExecutionImports:
    """Fixed policy reader plus the final H1 reliable runtime-import producer."""
    def __init__(self, orchestrator: Any, *, runtime_imports: RuntimeImportReader) -> None:
        if not callable(runtime_imports):
            raise ValueError("the original reliable runtime importer is required")
        self.orchestrator, self.store, self.runtime_imports = orchestrator, orchestrator.store, runtime_imports

    def read_runtime(self, store: Store, mission_id: str) -> CompleteRead[ImportedExecutionSource]:
        if store is not self.store:
            raise SourceUnavailable("taskgraph_execution_store_mismatch")
        return self.runtime_imports(store, mission_id)

    def read_execution_policy(self, store: Store, mission_id: str) -> CompleteRead[ImportedExecutionSource]:
        if store is not self.store:
            raise SourceUnavailable("taskgraph_execution_store_mismatch")
        with store.read_view() as db:
            orchestrator = self.orchestrator
            mission = store.get_mission(mission_id)
            policy_binding = store.get_mission_policy(mission_id)
            if mission is None or policy_binding is None or policy_binding.get("mission_id") != mission_id:
                raise SourceUnavailable("taskgraph_execution_policy_binding_missing")
            version_id = policy_binding.get("version_id")
            if not isinstance(version_id, str) or not version_id:
                raise SourceUnavailable("taskgraph_execution_policy_version_missing")
            version = store.get_policy_version(version_id)
            if (version is None or version.get("version_id") != version_id
                    or not isinstance(version.get("params"), Mapping)
                    or params_hash(version["params"]) != version.get("params_hash")):
                raise SourceUnavailable("taskgraph_execution_policy_version_invalid")
            # The runtime's resolved policy must be this same durable version;
            # cached or legacy configuration fallback is not an alternate source.
            if dict(orchestrator.policy_for(mission_id)) != dict(version["params"]):
                raise SourceUnavailable("taskgraph_execution_policy_cache_changed")
            config = orchestrator._config
            deployment = config.deployment_policy
            if not isinstance(deployment, DeploymentPolicy):
                raise SourceUnavailable("taskgraph_deployment_policy_missing")
            domain = orchestrator.commit.domain_for(mission_id)
            semantics = HtnStore(store)
            tasks = []
            for task in sorted(store.list_tasks(mission_id), key=lambda item: item.id):
                binding = semantics.task_semantics_of(mission_id, task.id)
                # Auxiliary original Tasks have their own permission intersection,
                # even though they are not semantic network members.
                role = orchestrator._template(role_for_task(task), mission_id)
                if binding is not None:
                    role = orchestrator._hierarchical_worker_template(role, mission_id)
                extra_tools = (tuple(config.agentdojo_tool_schemas) if domain.id == "agentdojo-v1"
                    else tuple(config.are_tool_schemas) if domain.id == "are-v1" else ())
                read_only = binding is not None and read_only_leaf(binding)
                selection_policy = orchestrator.commit.selection_policy_for(task.id)
                # The real dispatch path can replace the ordinary worker role
                # with SYNTHESIZER for a compare decision. Record both permission
                # inputs, never union their tools or choose a role here.
                roles = [("ordinary", role, role.prompt_version)]
                if selection_policy is not None:
                    selection_role = orchestrator._template(SYNTHESIZER, mission_id)
                    roles.append(("selection", selection_role, selection_role.prompt_version + ":compare-v2"))
                alternatives = []
                for mode, template, prompt_version in roles:
                    role_tools = (*template.tool_names, *extra_tools)
                    alternatives.append({"mode": mode, "role": template.name,
                        "prompt_version": prompt_version, "role_tools": list(role_tools),
                        "effective_tools": list(effective_tools(mission_tools=mission.allowed_tools,
                            task_tools=task.allowed_tools, role_tools=role_tools, deployment=deployment,
                            read_only_leaf=read_only))})
                tasks.append({"task_id": task.id,
                    "task_tools": list(task.allowed_tools), "permission_inputs": alternatives,
                    "paused": task.paused, "pause_reason": task.pause_reason,
                    "read_only_leaf": read_only, "selection_policy": selection_policy,
                    "budget": task.budget.to_json()})
            profiles = []
            for identity, profile in sorted(orchestrator._profiles.items()):
                if profile.profile_id != identity:
                    raise SourceUnavailable("taskgraph_execution_profile_identity_invalid")
                # Explicit declaration fields only; never serialize a provider,
                # connector implementation, tokenizer or credential-bearing repr.
                profiles.append({**profile.to_json(),
                    "price_table": None if profile.price_table is None else {
                        "snapshot_id": profile.price_table.snapshot_id,
                        "input_micros_per_million_tokens": profile.price_table.input_micros_per_million_tokens,
                        "output_micros_per_million_tokens": profile.price_table.output_micros_per_million_tokens}})
            body: dict[str, Any] = {"mission_id": mission_id,
                "mission": {"tenant_id": mission.tenant_id,
                    "status": str(mission.status), "allowed_tools": list(mission.allowed_tools)},
                "policy_binding": dict(policy_binding), "policy_version": dict(version),
                "deployment": deployment.to_json(), "tasks": tasks, "profiles": profiles,
                "routing_inputs": {"deployment": orchestrator._model_router.rules.to_json(),
                    "selected_profile": orchestrator._selected_profile(mission_id),
                    "frozen_default": orchestrator._frozen_default_route(mission_id),
                    "task_kind": (mission.final_report or {}).get("task_kind") or "code"},
                "resource_limits": {name: _document(getattr(config, name)) for name in (
                    "max_running_attempts", "max_pending_dispatch", "max_pending_verifications",
                    "max_model_calls_per_turn", "max_tool_calls_per_turn", "turn_deadline_seconds")}}
            source = ImportedExecutionSource(mission_id=mission_id, kind="execution_policy",
                canonical_document=canonical_json(body))
            sequence = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?", (mission_id,)).fetchone()[0]
            return CompleteRead(value=source, source_id=f"{mission_id}:execution-policy",
                source_digest=sha256_hex(source.to_json()), through_seq=sequence)

    def validate_attempt(self, store: Store, task_id: str, policy_source: CompleteRead[ImportedExecutionSource],
                         intent_config: Mapping[str, Any]) -> None:
        """Narrow the actual frozen Worker request inside original Attempt creation.

        Original budget reservation, selection admission, materialization and
        Provider/Tool/Action authorization still run in their existing entry points.
        """
        self._validate_request(store, task_id, policy_source, intent_config, creating=True)

    def validate_handoff(self, store: Store, intent: DispatchIntent) -> None:
        """Fresh permission intersection for a frozen request, without rebinding it."""
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_EXECUTION_RECHECK_TRANSACTION_REQUIRED")
        attempt = store.get_attempt(intent.subject_id)
        if (intent.kind != "attempt" or attempt is None or attempt.mission_id != intent.mission_id
                or attempt.status in TERMINAL_ATTEMPT):
            raise StoreConflict("TASKGRAPH_EXECUTION_ATTEMPT_STOPPED")
        self._validate_request(store, attempt.task_id,
            self.read_execution_policy(store, intent.mission_id), intent.config, creating=False)

    def _validate_request(self, store: Store, task_id: str, policy_source: CompleteRead[ImportedExecutionSource],
                          intent_config: Mapping[str, Any], *, creating: bool) -> None:
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_EXECUTION_RECHECK_TRANSACTION_REQUIRED")
        task = store.get_task(task_id)
        if task is None:
            raise StoreError("TASKGRAPH_EXECUTION_TASK_MISSING")
        mission = store.get_mission(task.mission_id)
        if mission is None or str(mission.status) != "ACTIVE" or task.paused or task.status in TERMINAL_TASK:
            raise StoreConflict("TASKGRAPH_EXECUTION_NOT_ACTIVE")
        if policy_source.value.mission_id != task.mission_id or policy_source.value.kind != "execution_policy":
            raise StoreError("TASKGRAPH_EXECUTION_POLICY_SOURCE_MISMATCH")
        body = policy_source.value.to_json()["document"]
        choices = [item for item in body["tasks"] if item["task_id"] == task_id]
        if len(choices) != 1:
            raise StoreError("TASKGRAPH_EXECUTION_TASK_POLICY_MISSING")
        current = choices[0]
        mode = "selection" if intent_config.get("selection_decision_id") is not None else "ordinary"
        variants = [item for item in current["permission_inputs"] if item["mode"] == mode
                    and item["role"] == intent_config.get("role")
                    and item["prompt_version"] == intent_config.get("prompt_version")]
        frozen_version = intent_config.get("task_version")
        if (len(variants) != 1 or type(frozen_version) is not int
                or frozen_version > task.version or (creating and frozen_version != task.version)):
            raise StoreConflict("TASKGRAPH_EXECUTION_ROLE_OR_TASK_CHANGED")
        if intent_config.get("policy_version_id") != body["policy_binding"]["version_id"]:
            raise StoreConflict("TASKGRAPH_EXECUTION_POLICY_CHANGED")
        agent_json = intent_config.get("agent_config")
        if not isinstance(agent_json, Mapping):
            raise StoreError("TASKGRAPH_EXECUTION_AGENT_CONFIG_MISSING")
        agent = AgentConfig.from_json(dict(agent_json))
        offered = intent_config.get("allowed_tools")
        if (not isinstance(offered, (list, tuple)) or tuple(offered) != agent.tool_names
                or not set(agent.tool_names) <= set(variants[0]["effective_tools"])
                or bool(intent_config.get("read_only_leaf", False)) != current["read_only_leaf"]):
            raise StoreConflict("TASKGRAPH_EXECUTION_PERMISSION_CHANGED")
        profile_id = intent_config.get("runtime_profile_id")
        profile = self.orchestrator._profiles.get(profile_id)
        if (profile is None or agent.model_profile_ref != profile_id
                or profile.model != intent_config.get("model")
                or profile.context_snapshot() != intent_config.get("runtime_context")):
            raise StoreConflict("TASKGRAPH_EXECUTION_PROFILE_CHANGED")
        selected = body["routing_inputs"]["selected_profile"]
        if selected is not None and selected != profile_id:
            raise StoreConflict("TASKGRAPH_EXECUTION_MISSION_PROFILE_CHANGED")
        guard = self.orchestrator._admission_for(profile_id)
        fingerprint = None if guard is None else guard.fingerprint
        if intent_config.get("provider_admission_fingerprint") != fingerprint:
            raise StoreConflict("TASKGRAPH_EXECUTION_PROVIDER_ADMISSION_CHANGED")
        limits = body["resource_limits"]
        tool_limit = limits["max_tool_calls_per_turn"]
        if task.budget.max_tool_calls is not None:
            tool_limit = min(tool_limit, task.budget.max_tool_calls)
        deadline = limits["turn_deadline_seconds"]
        if task.budget.max_runtime_seconds is not None:
            deadline = min(deadline, task.budget.max_runtime_seconds)
        if (agent.limits.max_model_calls_per_turn > limits["max_model_calls_per_turn"]
                or agent.limits.max_tool_calls_per_turn > tool_limit
                or intent_config.get("max_tool_calls") != agent.limits.max_tool_calls_per_turn
                or agent.limits.turn_deadline_seconds > deadline):
            raise StoreConflict("TASKGRAPH_EXECUTION_LIMITS_EXCEEDED")
