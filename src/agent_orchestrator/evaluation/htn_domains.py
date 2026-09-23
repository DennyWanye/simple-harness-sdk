# SPDX-License-Identifier: Apache-2.0
"""Concrete H8 deployment adapters; domain selection stays outside the HTN core."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts.models import ContractError
from ..contracts.semantic_base import VersionedRef, content_hash_of
from ..domains.drone_sim import DroneSimulator, deployment, planning_world
from ..governance.permissions import Principal
from ..governance.policies import deployed_layers
from ..planning.htn.observers.code import code_observers
from ..planning.htn.seed_methods.loader import load_domain, seed_content_hash
from ..planning.htn.world import build_planning_world
from ..runtime.assembly import OrchestratorConfig
from ..runtime.sandbox import resolve_executor
from ..runtime.tool_gateway import read_tool_schemas
from ..storage.htn_store import HtnStore
from .appworld import AppWorldConfig, AppWorldEpisode
from .htn_hierarchical import HierarchicalExecution, prepare_root
from .htn_matrix import EvidenceFile, H8Manifest, H8Run
from .htn_oracles import grade_appworld, grade_code, grade_drone

BASE_TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list")
EXECUTOR_VERSION = "h8-runtime-original-commit-process-recovery-v2"

SEEDS = {"code-v1": "code", "appworld-v1": "appworld-operation-v1", "drone-sim-v1": "drone-sim-v1"}


class EpisodeDomain:
    def __init__(self, manifest: H8Manifest, run: H8Run, root: Path,
                 config: OrchestratorConfig, *, appworld_url: str | None = None,
                 large_context: bool = False, resume: bool = False):
        self.manifest, self.run, self.root = manifest, run, root
        self.fixture = run.scenario.fixture
        self.seed = dict(self.fixture.get("files", {}))
        self.simulator: DroneSimulator | None = None
        self.episode: AppWorldEpisode | None = None
        self.config = config
        self.connectors: dict[str, Any] = {}
        self.operation_profiles: Any = None
        self.completion_contract = manifest.domain_deployments[run.scenario.domain]["completion_contract"]
        self.tools: tuple[str, ...]
        self.criteria: tuple[str, ...]
        self.repository = root / "source-repository"
        if run.scenario.domain == "code-v1":
            self.tools = (*BASE_TOOLS, "run_tests")
            self._code_repository(resume=resume)
            self.parameters = {"repository": str(self.repository), "failing_test": self.fixture["failing_test"]}
            self.criteria = ("pytest:tests/test_target.py", "The requested implementation satisfies the stated contract.")
        elif run.scenario.domain == "drone-sim-v1":
            self.simulator = DroneSimulator(root / "drone.sqlite")
            self.config = deployment(config, self.simulator)
            self.tools = (*BASE_TOOLS, "drone_sim_telemetry", "drone_sim_command")
            self.parameters = {"vehicle": self.fixture["vehicle"], **self.fixture["point"]}
            self.criteria = ("The simulator captured at the requested coordinates and is landed.",)
        else:
            if not appworld_url:
                raise ContractError("AppWorld requires the actual external environment service")
            self.tools = (*BASE_TOOLS, "appworld_execute")
            self.parameters = {"app": self.fixture["app"], "request": self.fixture["goal"],
                               "target": self.fixture["task_id"]}
            self.criteria = ("The user's application request is fully fulfilled with actual effects checked.",
                             "action:appworld_sandbox.execute:" + self.fixture["task_id"])
            contract = self.completion_contract
            if (not isinstance(contract, dict) or set(contract) != {"mode", "by_task"}
                    or contract["mode"] != "REQUIRED_EFFECTS"):
                raise ContractError("AppWorld requires explicitly approved completion contracts by task")
            self.completion_contract = contract["by_task"][self.fixture["task_id"]]
        if not set(self.tools) <= set(self.config.deployment_policy.allowed_tools):
            raise ContractError("domain tools are not allowed by this deployment")
        schemas = {**read_tool_schemas(large=large_context),
                   **{name: tool.schema for name, tool in self.config.domain_tools.items()}}
        self.tool_schemas = {name: schemas[name] for name in self.tools}
        frozen = manifest.domain_deployments[run.scenario.domain]
        package = load_domain(SEEDS[run.scenario.domain]).planning_package()
        if (content_hash_of(self.tool_schemas) != content_hash_of(frozen["tool_schemas"])
                or package.content_hash != frozen["domain_package_hash"]):
            raise ContractError("installed domain package/tool schemas differ from H8 manifest")
        actual_environment = environment_identity(run.scenario.domain, self.config, appworld_url=appworld_url)
        if frozen["environment_identity"] != actual_environment or frozen["executor_version"] != EXECUTOR_VERSION:
            raise ContractError("actual domain environment/executor differs from H8 manifest")
        if run.scenario.domain == "appworld-v1":
            episode_config = AppWorldConfig(task_id=str(self.fixture["task_id"]),
                experiment_name="h8-" + run.run_id, remote_environment_url=appworld_url, random_seed=run.seed)
            if resume:
                from .appworld_resume import attach_episode
                self.episode = attach_episode(episode_config, root / "appworld-resume.json")
            else:
                self.episode = AppWorldEpisode(episode_config)
            try:
                if (self.episode.agent.instruction != self.fixture["dataset_instruction"]
                        or self.episode.dataset_identity() != self.fixture["dataset_hash"]):
                    raise ContractError("installed AppWorld instruction/inputs differ from the frozen dataset task")
                self.config = replace(config, appworld_execute=self.episode.agent.execute)
                from .appworld_operations import AppWorldOperationConnector
                from .appworld_operation_profiles import AppWorldOperationProfiles
                from .appworld_state_observations import AppWorldStatePolicy
                connector = AppWorldOperationConnector(self.episode, root / "appworld-operation-journal", create=not resume)
                self.connectors[connector.name] = connector
                self.operation_profiles = AppWorldOperationProfiles(connector,
                    AppWorldStatePolicy.from_json(self.fixture["state_policy"]))
                for effect in self.completion_contract["effects"]:
                    if (effect["milestone_policy_ref"] != self.operation_profiles.milestone_policy_ref.to_json()
                            or effect["evidence_policy_ref"] != self.operation_profiles.evidence_policy_ref.to_json()):
                        raise ContractError("AppWorld contract policy differs from registered state readback")
            except BaseException as error:
                try:
                    self.episode.abort()
                except BaseException as cleanup_error:
                    error.add_note(f"AppWorld setup cleanup failed: {type(cleanup_error).__name__}")
                raise

    def _code_repository(self, *, resume: bool) -> None:
        if self.repository.exists():
            if not resume or not (self.repository / ".git").is_dir():
                raise ContractError("existing code fixture needs its original recovery identity")
            for name, body in self.seed.items():
                if (self.repository / name).read_text() != body:
                    raise ContractError("the source evidence fixture was changed outside the worker workspace")
            return
        if resume:
            raise ContractError("source repository is missing during recovery")
        self.repository.mkdir(parents=True)
        for name, body in self.seed.items():
            path = (self.repository / name).resolve()
            if not path.is_relative_to(self.repository.resolve()):
                raise ContractError("fixture path escapes its repository")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        for args in (("git", "init", "-q"), ("git", "add", "--all"),
                     ("git", "-c", "user.name=H8 fixture", "-c", "user.email=h8@localhost", "commit", "-qm", "Frozen H8 source fixture")):
            subprocess.run(args, cwd=self.repository, check=True, capture_output=True, timeout=30)

    def prepare(self, loop: Any, mission: Any, *, principal: Principal) -> None:
        htn = HtnStore(loop.store)
        if self.simulator is not None:
            self.simulator.create_episode(mission.id, vehicle=self.fixture["vehicle"], battery=self.fixture["battery"])
            world = planning_world(self.simulator, mission.id, semantics=htn)
        else:
            from ..planning.htn.observers.appworld import appworld_observers
            kwargs = ({"observers": code_observers(self.repository, allow_test_execution=True)}
                      if self.episode is None else {"observers": appworld_observers(self.episode, scope_id="mission")})
            world = build_planning_world(mission.id, semantics=htn,
                domains=(SEEDS[self.run.scenario.domain],),
                deployed_layers=deployed_layers(self.config.deployment_policy), **kwargs)
        loop.install_hierarchical(planning=world)
        name = str(self.fixture["goal_type"])
        version = int(self.fixture.get("goal_version", 1))
        prepare_root(loop, mission, world, task_type=VersionedRef(name, version, seed_content_hash(name, version)),
            parameters=self.parameters, principal=principal,
            recursion_fuel=int(self.manifest.domain_deployments[self.run.scenario.domain]["recursion_fuel"]),
            completion_contract=self.completion_contract)

    def prepare_single(self) -> None:
        if self.simulator is not None:
            self.simulator.create_episode(self.run.run_id, vehicle=self.fixture["vehicle"], battery=self.fixture["battery"])

    def accepted_workspace(self, execution: HierarchicalExecution) -> Path:
        from ..artifacts.workspace import WorkspaceManager
        files: dict[str, bytes] = {}
        for artifact in execution.accepted_artifacts:
            path = str(artifact["path"])
            raw = Path(str(artifact["storage_uri"])).read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact["content_hash"] or len(raw) != artifact["size_bytes"]:
                raise ContractError("accepted artifact no longer matches its original content identity")
            if path in files and files[path] != raw:
                raise ContractError("accepted outputs contain conflicting versions of one path")
            files[path] = raw
        # Original integrated-copy handles path confinement and seeds the oracle.
        return WorkspaceManager(self.root / "oracle-workspaces").integrated_copy(
            "accepted", seed=self.seed, files=files).root

    async def grade(self, *, workspace: Path | None, mission_id: str) -> tuple[bool, EvidenceFile]:
        if self.simulator is not None:
            return grade_drone(self.run.scenario, database=self.simulator.database, mission_id=mission_id, root=self.root)
        if self.episode is not None:
            return grade_appworld(self.run.scenario, self.episode, self.root)
        if workspace is None:
            raise ContractError("code evaluation requires the actual final workspace")
        return await grade_code(self.run.scenario, workspace, self.root,
            executor=resolve_executor(self.config.deployment_policy, self.config.sandbox_executor))

    def close(self) -> None:
        if self.episode is not None:
            self.episode.close()


def environment_identity(domain: str, config: OrchestratorConfig, *, appworld_url: str | None = None) -> str:
    """Hash actual deployment facts, independent of arm and scenario state."""
    facts: dict[str, Any]
    if domain == "code-v1":
        executor = resolve_executor(config.deployment_policy, config.sandbox_executor)
        if executor is None:
            raise ContractError("code H8 requires the deployed code executor")
        facts = {"kind": executor.kind, "environment_digest": executor.environment_digest,
                 "interpreter": executor.interpreter}
    elif domain == "drone-sim-v1":
        import inspect
        source = inspect.getsourcefile(DroneSimulator)
        if source is None:
            raise ContractError("drone simulator source identity is unavailable")
        facts = {"simulator_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(), "simulated": True}
    elif domain == "appworld-v1":
        from importlib.metadata import version
        if not appworld_url:
            raise ContractError("AppWorld service identity is unavailable")
        facts = {"package_version": version("appworld"), "service": appworld_url.rstrip("/")}
    else:
        raise ContractError("unknown H8 domain environment")
    return content_hash_of({"domain": domain, "facts": facts})
