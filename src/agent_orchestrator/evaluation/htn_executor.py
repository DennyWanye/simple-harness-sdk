# SPDX-License-Identifier: Apache-2.0
"""The H8 four-arm executor: real runtimes, one physical meter, external grading."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Protocol

from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..governance.permissions import Principal
from ..planning.htn.cross_domain_acceptance import FourArm, ScenarioKind
from ..runtime.assembly import OrchestratorConfig
from ..runtime.model_router import RuntimeProfile
from .htn_domains import EpisodeDomain
from .htn_hierarchical import execute_hierarchical
from .htn_matrix import EpisodeReceipt, EvidenceFile, H8Manifest, H8MeterContext, H8Run
from .htn_meter import DurableMeteredProvider
from .htn_oracles import write_evidence
from .htn_single_agent import execute_single_agent
from .htn_recovery import RuntimeRecovery, RuntimeRestartRequested
from .htn_interventions import ToolIntervention


class EpisodeIntervention(Protocol):
    """A real intervention controller, never an evaluator returning a boolean."""
    async def before_cycle(self, loop: Any, mission: Any) -> None: ...
    def configure(self, config: OrchestratorConfig, domain: EpisodeDomain) -> OrchestratorConfig: ...
    def receipt(self, root: Path) -> EvidenceFile: ...


def source_fingerprint(checkout: Path) -> str:
    rows = []
    for path in sorted((checkout / "src").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json"}:
            rows.append([path.relative_to(checkout).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    for name in ("pyproject.toml", "uv.lock"):
        path = checkout / name
        rows.append([name, hashlib.sha256(path.read_bytes()).hexdigest()])
    return content_hash_of(rows)


class RuntimeEpisodeExecutor:
    def __init__(self, *, provider: Any, config: OrchestratorConfig, checkout: Path,
                 estimate_input_tokens: Callable[..., int], tokenizer: Any = None,
                 context_policy: Any = None, extra_input_reserve: Callable[..., int] | None = None,
                 solver: Any = None, solver_limits: Any = None, appworld_url: str | None = None,
                 interventions: Callable[[H8Run, Path], EpisodeIntervention] | None = None):
        self.process_recovery = False
        self.provider, self.config, self.checkout = provider, config, checkout
        self.estimate, self.extra = estimate_input_tokens, extra_input_reserve
        self.tokenizer, self.context_policy = tokenizer, context_policy
        self.solver, self.solver_limits, self.appworld_url = solver, solver_limits, appworld_url
        self.interventions = interventions or ToolIntervention

    def preflight(self, manifest: H8Manifest) -> None:
        """Check service registration before spending any physical model budget."""
        import json
        from urllib.request import urlopen
        from .appworld_state_observations import AppWorldStatePolicy
        from .appworld_api_observations import digest
        if not self.appworld_url:
            raise ContractError("H8 AppWorld service is not configured")
        expected = sorted({AppWorldStatePolicy.from_json(s.fixture["state_policy"]).content_hash
                           for s in manifest.scenarios if s.domain == "appworld-v1"})
        with urlopen(self.appworld_url.rstrip("/") + "/host_state_policy_registry", timeout=10) as response:
            raw = response.read(65537)
        if len(raw) > 65536 or json.loads(raw) != {"policy_hashes": expected, "registry_hash": digest(expected)}:
            raise ContractError("AppWorld service has not registered this frozen batch's exact state policies")

    async def execute(self, manifest: H8Manifest, run: H8Run, root: Path, *, resume: bool) -> EpisodeReceipt:
        if source_fingerprint(self.checkout) != manifest.source_fingerprint:
            raise ContractError("H8 source changed since the manifest was frozen")
        from .htn_identity import context_profile_identity, runtime_config_identity
        deployed = manifest.domain_deployments[run.scenario.domain]
        reserve = getattr(self, "extra_input_reserve_tokens", 0)
        if (deployed.get("runtime_config_hash") != runtime_config_identity(self.config, self.solver, self.solver_limits)
                or deployed.get("context_profile_identity") != context_profile_identity(
                    self.tokenizer, self.context_policy, self.provider, reserve)):
            raise ContractError("H8 actual runtime/context configuration differs from its frozen identity")

        def frozen_reserve(request: Any) -> int:
            actual = 0 if self.extra is None else self.extra(request)
            if type(actual) is not int or actual != reserve:
                raise ContractError("H8 actual token reserve differs from its frozen value")
            return actual
        if run.scenario.kind not in {ScenarioKind.NORMAL, ScenarioKind.RECOVERY} and self.interventions is None:
            raise ContractError("non-normal H8 run requires its actual intervention controller")
        intervention: Any = None
        if run.scenario.kind == ScenarioKind.RECOVERY:
            if not self.process_recovery:
                raise ContractError("H8 recovery must run through the process supervisor")
            intervention = RuntimeRecovery(run, root,
                process_recovery=getattr(self, "process_recovery", False), manifest_hash=manifest.fingerprint)
        elif run.scenario.kind != ScenarioKind.NORMAL:
            assert self.interventions is not None
            intervention = self.interventions(run, root)
        config = replace(self.config, evidence_root=root / "runtime",
            hierarchical_repair_enabled=run.arm != FourArm.H_NATIVE_NO_REPAIR,
            planning_backend=self.solver if run.arm == FourArm.H_SOLVER else None,
            planning_backend_limits=self.solver_limits if run.arm == FourArm.H_SOLVER else None)
        context = H8MeterContext(manifest, run, lambda counts: None)
        meter = DurableMeteredProvider(self.provider, context, estimate_input_tokens=self.estimate,
            extra_input_reserve=frozen_reserve, checkpoint=root / "physical-meter.json",
            run_identity=content_hash_of([manifest.fingerprint, run.run_id]), resume=resume)
        domain = EpisodeDomain(manifest, run, root, config, appworld_url=self.appworld_url,
                               large_context=self.context_policy is not None, resume=resume)
        try:
            config = domain.config
            if intervention is not None:
                config = intervention.configure(config, domain)
                if isinstance(intervention, RuntimeRecovery):
                    intervention.bind_meter(meter)
                    if intervention.process_recovery and resume:
                        intervention.reopened(meter)
            principal = Principal("h8-frozen-experiment-issuer")
            commands = None
            if domain.episode is not None and run.arm != FourArm.STRONG_SINGLE_AGENT:
                from .htn_operation_controller import AppWorldExperimentCommands
                commands = AppWorldExperimentCommands(principal=principal, contract=domain.completion_contract)

            async def before_cycle(loop: Any, mission: Any) -> None:
                if intervention is not None:
                    await intervention.before_cycle(loop, mission)
                if commands is not None:
                    commands.advance(loop, mission)
            formal = None
            statuses: tuple[str, ...] = ()
            unresolved = critical = 0
            workspace = None
            while True:
                try:
                    if run.arm == FourArm.STRONG_SINGLE_AGENT:
                        domain.prepare_single()
                        single = await execute_single_agent(manifest=manifest, run=run, config=config,
                            provider=meter, root=root / "runtime", prompt=str(run.scenario.fixture["goal"]),
                            seed=domain.seed, allowed_tools=domain.tools, tokenizer=self.tokenizer,
                            context_policy=self.context_policy, resume=resume,
                            on_tick=getattr(intervention, "before_single_tick", None),
                            instructions=("Complete the entire user's task using the deployed tools. Inspect actual results, "
                                "recover from failures within the same budget, and verify the final state. "
                                "Never alter tests or claim success without evidence. Report completed and unfinished work."))
                        runtime_body = asdict(single)
                        runtime_body["workspace"] = str(single.workspace)
                        workspace, mission_id = single.workspace, run.run_id
                        terminal = "COMPLETED" if single.outcome.get("state") == "committed" and single.outcome.get("public_output") else "FAILED"
                    else:
                        profile = RuntimeProfile("default", meter, manifest.model, provider_kind="real",
                            tokenizer=self.tokenizer, context_policy=self.context_policy,
                            max_concurrent_model_calls=manifest.physical_slots,
                            default_max_output_tokens=config.default_max_output_tokens,
                            max_output_tokens_ceiling=config.max_output_tokens_ceiling)
                        executed = await execute_hierarchical(manifest=manifest, run=run, config=config,
                            provider=meter, root=root / "runtime", seed=domain.seed, allowed_tools=domain.tools,
                            success_criteria=domain.criteria, principal=principal, resume=resume, runtime_profile=profile,
                            prepare=lambda loop, mission: domain.prepare(loop, mission, principal=principal),
                            on_cycle=before_cycle, connectors=domain.connectors,
                            operation_profiles=domain.operation_profiles)
                        mission_id, terminal = executed.mission_id, executed.terminal_status
                        formal, statuses = executed.formal_completion_id, executed.solver_statuses
                        unresolved, critical = executed.unresolved_operations, executed.critical_effect_failures
                        runtime_body = dict(executed.receipt)
                        if run.scenario.domain == "code-v1":
                            workspace = domain.accepted_workspace(executed)
                    break
                except RuntimeRestartRequested:
                    if not isinstance(intervention, RuntimeRecovery):
                        raise
                    # The helper has already closed the original runtime. Re-read
                    # its durable physical accounting; never reset the domain host.
                    meter = DurableMeteredProvider(self.provider, context,
                        estimate_input_tokens=self.estimate, extra_input_reserve=frozen_reserve,
                        checkpoint=root / "physical-meter.json",
                        run_identity=content_hash_of([manifest.fingerprint, run.run_id]), resume=True)
                    intervention.reopened(meter)
                    resume = True
            domain_success, score = await domain.grade(workspace=workspace, mission_id=mission_id)
            elapsed = max(0.0, manifest.budget.seconds + time.monotonic() - meter._deadline)
            runtime_body.update(physical_counters=asdict(meter.counters), unknown_usage_calls=meter.unknown_usage_calls,
                physical_calls=meter.observations, admission_denials=meter.admission_denials,
                tools_hash=content_hash_of(domain.tool_schemas), manifest_hash=manifest.fingerprint,
                run_id=run.run_id)
            runtime_ref = write_evidence(root, "runtime-receipt.json", runtime_body)
            injected = None if intervention is None else intervention.receipt(root)
            import json
            triggered = injected is not None and json.loads((root / injected.relative_path).read_text()).get("triggered") is True
            return EpisodeReceipt(run.run_id, manifest.fingerprint, terminal, domain_success, score, runtime_ref,
                meter.target.provider_id, meter.target.model, content_hash_of(domain.tool_schemas), meter.counters,
                unresolved, critical, meter.unknown_usage_calls, elapsed, formal, injected, statuses,
                intervention_triggered=triggered)
        finally:
            domain.close()
