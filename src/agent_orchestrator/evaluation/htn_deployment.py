# SPDX-License-Identifier: Apache-2.0
"""Configured real-DeepSeek deployment factory for the H8 batch runner.

Set ``H8_DEPLOYMENT_CONFIG`` to one JSON file.  Provider credentials are read
only through the environment variable named by that file and never enter the
manifest.  Constructing the deployment performs no Provider or AppWorld call.
"""
from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.providers import OpenAICompatibleProvider, Secret

from ..contracts.models import ContractError
from ..domains.drone_sim import DroneSimulator
from ..domains.drone_sim import deployment as drone_deployment
from ..governance.policies import DeploymentPolicy
from ..planning.htn.backend_port import PandaPlanningBackend, PlanningLimits
from ..planning.htn.backends.up_aries import UpAriesPlanningBackend
from ..planning.htn.seed_methods.loader import load_domain
from ..runtime.assembly import OrchestratorConfig
from ..runtime.deepseek_tokens import DeepSeekV41TokenEstimator
from ..runtime.tool_gateway import read_tool_schemas
from .experiment import ExperimentBudget
from .htn_domains import BASE_TOOLS, EXECUTOR_VERSION, SEEDS, environment_identity
from .htn_executor import RuntimeEpisodeExecutor, source_fingerprint
from .htn_identity import context_profile_identity, runtime_config_identity
from .htn_interventions import ToolIntervention
from .htn_matrix import DOMAINS, H8Manifest
from .htn_scenarios import build_scenarios

CONFIG_ENV = "H8_DEPLOYMENT_CONFIG"


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"H8 deployment {name} must be an object")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"H8 deployment {name} must be explicit")
    return value.strip()


def _load_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"unable to read H8 deployment {name}") from error


def _configured_path() -> Path:
    value = os.environ.get(CONFIG_ENV)
    if not value:
        raise ContractError(f"{CONFIG_ENV} must name the explicit H8 deployment JSON")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{CONFIG_ENV} does not name a file")
    return path


def _import_attribute(spec: str, name: str) -> Any:
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ContractError(f"H8 deployment {name} must use module:attribute syntax")
    try:
        return getattr(importlib.import_module(module), attribute)
    except Exception as error:
        raise ContractError(f"unable to load H8 deployment {name} ({type(error).__name__})") from error


def _solver(raw: Mapping[str, Any]) -> tuple[Any, PlanningLimits]:
    config = dict(raw)
    kind = _text(config.pop("kind", None), "solver.kind")
    limits_raw = _mapping(config.pop("limits", None), "solver.limits")
    if kind == "up-aries":
        if config:
            raise ContractError("up-aries solver has unknown configuration")
        backend: Any = UpAriesPlanningBackend()
    elif kind == "panda":
        toolchain_factory = _import_attribute(
            _text(config.pop("toolchain_factory", None), "solver.toolchain_factory"),
            "solver.toolchain_factory",
        )
        if config:
            raise ContractError("panda solver has unknown configuration")
        backend = PandaPlanningBackend(toolchain_factory())
    else:
        raise ContractError("solver.kind must be up-aries or panda")
    try:
        limits = PlanningLimits(**limits_raw)
    except TypeError as error:
        raise ContractError("solver limits are malformed") from error
    return backend, limits


def _provider(raw: Mapping[str, Any]) -> tuple[Any, DeepSeekV41TokenEstimator, str]:
    config = dict(raw)
    base_url = _text(os.environ.get(_text(config.pop("base_url_env", None), "provider.base_url_env")),
                     "configured provider base URL")
    model = _text(os.environ.get(_text(config.pop("model_env", None), "provider.model_env")),
                  "configured provider model")
    key = _text(os.environ.get(_text(config.pop("api_key_env", None), "provider.api_key_env")),
                "configured provider credential")
    provider_id = _text(config.pop("provider_id", None), "provider.provider_id")
    tokenizer_path = Path(_text(config.pop("tokenizer_path", None), "provider.tokenizer_path")).expanduser().resolve()
    timeout = config.pop("timeout_seconds", None)
    tool_schema_mode = _text(config.pop("tool_schema_mode", None), "provider.tool_schema_mode")
    trust_env = config.pop("trust_env", False)
    stream = config.pop("stream", False)
    if (config or type(stream) is not bool or type(trust_env) is not bool
            or type(timeout) not in (int, float) or timeout <= 0):
        raise ContractError("provider configuration contains unknown or invalid fields")
    counter = DeepSeekV41TokenEstimator(tokenizer_path, model=model, tool_schema_mode=tool_schema_mode)
    provider = OpenAICompatibleProvider(
        httpx.AsyncClient(trust_env=trust_env),
        base_url,
        model,
        Secret(key),
        timeout=timeout,
        provider_id=provider_id,
        tool_schema_mode=tool_schema_mode,
        stream=stream,
    )
    return provider, counter, model


def build_deployment() -> tuple[RuntimeEpisodeExecutor, H8Manifest]:
    """Build the exact executor/manifest pair consumed by ``htn_batch``."""
    raw = _mapping(_load_json(_configured_path(), "config"), "config")
    required = {
        "experiment_id", "checkout", "appworld_tasks_json", "appworld_service_url", "appworld_data_root",
        "provider", "budget", "physical_slots", "repetitions", "seed", "solver",
        "recursion_fuel", "context_policy", "orchestrator", "completion_contracts",
        "extra_input_reserve_tokens",
    }
    if set(raw) != required:
        raise ContractError("H8 deployment config fields are incomplete or unknown")

    checkout = Path(_text(raw["checkout"], "checkout")).expanduser().resolve()
    if not (checkout / "src").is_dir():
        raise ContractError("H8 checkout has no SDK source tree")
    tasks_path = Path(_text(raw["appworld_tasks_json"], "appworld_tasks_json")).expanduser().resolve()
    tasks = _load_json(tasks_path, "AppWorld task list")
    if not isinstance(tasks, list):
        raise ContractError("AppWorld task list must be a JSON array")
    scenarios = build_scenarios(appworld_tasks=tasks)
    appworld_url = _text(raw["appworld_service_url"], "appworld_service_url")
    data_root = Path(_text(raw["appworld_data_root"], "appworld_data_root")).expanduser().resolve()
    if not (data_root / "data" / "tasks").is_dir():
        raise ContractError("AppWorld data root has no installed task dataset")
    from appworld import update_root  # type: ignore[import-not-found]
    update_root(str(data_root))

    provider, counter, model = _provider(_mapping(raw["provider"], "provider"))
    solver, solver_limits = _solver(_mapping(raw["solver"], "solver"))
    try:
        budget = ExperimentBudget(**_mapping(raw["budget"], "budget"))
        context_policy = ContextPolicy(**_mapping(raw["context_policy"], "context_policy"))
    except TypeError as error:
        raise ContractError("H8 budget/context policy is malformed") from error

    orchestrator = _mapping(raw["orchestrator"], "orchestrator")
    allowed_orchestrator = {
        "max_concurrency", "default_max_output_tokens", "max_output_tokens_ceiling",
        "test_timeout_seconds", "turn_deadline_seconds", "max_model_calls_per_turn",
        "max_tool_calls_per_turn", "attempt_reserve_tokens", "planner_reserve_tokens",
        "critic_reserve_tokens", "manager_reserve_tokens",
    }
    if set(orchestrator) - allowed_orchestrator:
        raise ContractError("orchestrator configuration contains unsupported fields")
    physical_slots = raw["physical_slots"]
    if type(physical_slots) is not int or physical_slots < 1:
        raise ContractError("physical_slots must be a positive integer")
    base_config = OrchestratorConfig(
        evidence_root=checkout / ".local-test-evidence" / "h8-runtime-placeholder",
        model=model,
        max_concurrent_model_calls=physical_slots,
        deployment_policy=DeploymentPolicy(enabled_connectors=("appworld_sandbox",),
            enabled_event_operations=("appworld_sandbox.execute",)),
        **orchestrator,
    )
    # This local SQLite file is only used to obtain the exact deployed simulator
    # tool objects/schemas. Episodes create their own databases under evidence.
    identity_simulator = DroneSimulator(
        checkout / ".local-test-evidence" / "h8-deployment-identity" / "drone.sqlite"
    )
    runtime_config = drone_deployment(base_config, identity_simulator)
    # Each domain's operators get only that domain's concrete tools. AppWorld
    # leaves prepare files; its actual effects run through the root Operation.
    # A newly proposed/unregistered operator gets no tools in this deployment.
    from dataclasses import replace
    operator_tools = {"code-v1": (*BASE_TOOLS, "run_tests"), "appworld-v1": BASE_TOOLS,
                      "drone-sim-v1": (*BASE_TOOLS, "drone_sim_telemetry", "drone_sim_command")}
    allowlists = []
    for domain in DOMAINS:
        for definition in load_domain(SEEDS[domain]).task_types:
            operator = definition.operator_ref
            if operator is not None:
                key = f"{operator.id}@{operator.version}:{operator.content_hash}"
                entry = (key, operator_tools[domain])
                if entry not in allowlists:
                    allowlists.append(entry)
    runtime_config = replace(runtime_config, deployment_policy=replace(runtime_config.deployment_policy,
        operator_tool_allowlists=tuple(allowlists), require_operator_tool_policy=True))
    runtime_config_hash = runtime_config_identity(runtime_config, solver, solver_limits)
    reserve = raw["extra_input_reserve_tokens"]
    context_identity = context_profile_identity(counter, context_policy, provider, reserve)
    schemas = {
        **read_tool_schemas(large=True),
        **{name: tool.schema for name, tool in runtime_config.domain_tools.items()},
    }
    recursion = _mapping(raw["recursion_fuel"], "recursion_fuel")
    if set(recursion) != set(DOMAINS):
        raise ContractError("recursion_fuel must specify every H8 domain")
    raw_contracts = _mapping(raw["completion_contracts"], "completion_contracts")
    if set(raw_contracts) != set(DOMAINS):
        raise ContractError("completion_contracts must specify every H8 domain")
    completion_contracts: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        contract = _mapping(raw_contracts[domain], f"completion_contracts.{domain}")
        if not contract:
            raise ContractError("H8 completion contracts must be explicit")
        if domain == "appworld-v1" and contract.get("mode") != "REQUIRED_EFFECTS":
            raise ContractError("AppWorld H8 completion contract must use REQUIRED_EFFECTS")
        if domain == "appworld-v1":
            if (set(contract) != {"mode", "by_task"} or not isinstance(contract["by_task"], dict)
                    or set(contract["by_task"]) != {task["task_id"] for task in tasks}):
                raise ContractError("AppWorld must freeze an explicit completion contract for every actual task")
            if any(not isinstance(item, dict) or set(item) != {"mode", "content_criterion_ids", "effects"}
                   or item["mode"] != "REQUIRED_EFFECTS" for item in contract["by_task"].values()):
                raise ContractError("AppWorld task completion contract is malformed")
        # Canonical round-trip detaches the manifest from mutable caller input
        # without interpreting or inventing any effect slot.
        completion_contracts[domain] = json.loads(json.dumps(
            contract, sort_keys=True, ensure_ascii=True, allow_nan=False,
            separators=(",", ":"),
        ))
    domain_tools = {
        "code-v1": (*BASE_TOOLS, "run_tests"),
        "appworld-v1": (*BASE_TOOLS, "appworld_execute"),
        "drone-sim-v1": (*BASE_TOOLS, "drone_sim_telemetry", "drone_sim_command"),
    }
    deployments: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        deployments[domain] = {
            "tool_schemas": {name: schemas[name] for name in domain_tools[domain]},
            "domain_package_hash": load_domain(SEEDS[domain]).planning_package().content_hash,
            "environment_identity": environment_identity(
                domain, runtime_config, appworld_url=appworld_url
            ),
            "executor_version": EXECUTOR_VERSION,
            "recursion_fuel": recursion[domain],
            "runtime_config_hash": runtime_config_hash,
            "context_profile_identity": context_identity,
            "completion_contract": completion_contracts[domain],
        }
    manifest = H8Manifest(
        experiment_id=_text(raw["experiment_id"], "experiment_id"),
        provider=provider.target.provider_id,
        model=model,
        budget=budget,
        physical_slots=physical_slots,
        scenarios=scenarios,
        domain_deployments=deployments,
        source_fingerprint=source_fingerprint(checkout),
        solver_identity={"backend_id": solver.backend_id, "limits": solver_limits.to_json()},
        repetitions=raw["repetitions"],
        seed=raw["seed"],
    )
    executor = RuntimeEpisodeExecutor(
        provider=provider,
        config=runtime_config,
        checkout=checkout,
        estimate_input_tokens=counter.estimate_input_tokens,
        tokenizer=counter,
        context_policy=context_policy,
        extra_input_reserve=lambda request: reserve,
        solver=solver,
        solver_limits=solver_limits,
        appworld_url=appworld_url,
        interventions=ToolIntervention,
    )
    setattr(executor, "expected_runtime_config_identity", runtime_config_hash)
    setattr(executor, "expected_context_profile_identity", context_identity)
    setattr(executor, "extra_input_reserve_tokens", reserve)
    setattr(executor, "expected_completion_contracts", completion_contracts)
    return executor, manifest


__all__ = ("CONFIG_ENV", "build_deployment")
