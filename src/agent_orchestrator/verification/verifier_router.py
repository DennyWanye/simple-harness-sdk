# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Verifier Router (§14.1, ORCH §12.4, plan D9/D23).

The Task Contract's ``verification_policy`` names the layers that *must* run.
Layers run in the §14.1 order (format → rule → critic → test); the first required
layer that FAILs or ERRORs short-circuits the rest (D23).  Layers the policy did
not request are recorded as ``NOT_REQUIRED`` — never as PASS.  A layer that was
requested but could not run (e.g. the Critic's verdict was unreadable) is
``ERROR`` and blocks acceptance exactly like a FAIL: "未运行的必需层不能算 PASS".

The Critic layer is executed by a callback supplied by the orchestrator (it needs
a dispatch intent, budget and the Agent bridge); everything else is local.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..artifacts.workspace import Workspace
from ..contracts import Artifact, ContractError, Mission, ResultEnvelope, Task
from ..contracts.models import VERIFICATION_LAYERS
from ..governance.domains import CODE_PROFILE, DomainProfileV1
from ..memory.verified_knowledge import KnowledgeIndex
from .assessments import AssessmentBindingV1, citation_integrity, doc_rule_reusable
from .critics import CriticVerdict
from .deterministic_checks import (
    ERROR,
    FAIL,
    NOT_REQUIRED,
    PASS,
    LayerResult,
    code_test,
    format_check,
    rule_check,
)
from .evidence_resolver import EvidenceResolver
from .human_review import NEEDS_HUMAN, SUSPENDED, human_layer

CriticRunner = Callable[[str | None], Awaitable[CriticVerdict]]
VERIFIER_VERSION = "verifier-v1"  # step 6 (S6-09): recorded on every layer result


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    layers: tuple[LayerResult, ...]
    critic: CriticVerdict | None
    short_circuited_at: str | None
    suspended: bool = False  # step 7 (D7-8'): the sixth layer waits for a person

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "layers": [layer.to_json() for layer in self.layers],
            "critic": None if self.critic is None else self.critic.to_json(),
            "short_circuited_at": self.short_circuited_at,
            "suspended": self.suspended,
        }

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [layer.to_json() for layer in self.layers if layer.status in {FAIL, ERROR}]


class VerifierRouter:
    def __init__(
        self,
        *,
        test_timeout: float = 120.0,
        local_code_execution: bool = True,
        executor: Any = None,
        domain: Any = None,
    ) -> None:
        self._test_timeout = test_timeout
        self._local_code_execution = local_code_execution  # host support 0.9.8
        self._executor = executor  # P3.2 D2: what code_test runs through
        self._domain = domain  # P3.3 D1: the Mission's frozen domain profile

    async def verify(
        self,
        *,
        mission: Mission,
        task: Task,
        envelope: ResultEnvelope,
        artifacts: Sequence[Artifact],
        verification_copy: Workspace,
        client_result_id: str | None,
        run_critic: CriticRunner,
        recorder: Callable[[LayerResult], Awaitable[None]] | None = None,
        tampered: Sequence[str] = (),
        knowledge: KnowledgeIndex | None = None,
        require_synthesis_knowledge: bool = True,
        action_problems: Sequence[str] | None = None,
        human: Mapping[str, Any] | None = None,
        reuse: Mapping[str, LayerResult] | None = None,
        needs_human_allowed: bool = True,
        ablated: frozenset[str] = frozenset(),
        domain: DomainProfileV1 | None = None,
        assessment_binding: AssessmentBindingV1 | None = None,
        evidence_resolver: EvidenceResolver | None = None,
    ) -> Verdict:
        actual_domain = domain if domain is not None else self._domain
        document = actual_domain is not None and actual_domain.id != CODE_PROFILE.id
        required = set(task.verification_policy)
        if action_problems is not None:  # D7-2'': a result carrying actions/ is always rule-checked
            required.add("rule_check")
        if not self._local_code_execution and any(
            c.startswith("pytest:") for c in task.success_criteria
        ):
            # host support review round 2 P2-5: a Task from before the switch keeps a pytest
            # criterion nobody can judge here; the rule layer must run and FAIL it, whatever
            # the policy says — a Critic's PASS alone may never complete it
            required.add("rule_check")
        # step 8 (plan D8-7'): an ablation changes the effective policy explicitly — the layer
        # is not required in this run and says so; it is never a silent PASS
        removed = {layer for layer in ablated if layer in required}
        required -= removed
        # review P1-3 (§12.4): an ablation that leaves the Task no layer at all cannot pass
        emptied = bool(removed) and not required
        layers: list[LayerResult] = []
        critic: CriticVerdict | None = None
        short_at: str | None = None
        test_output: str | None = None
        escalated = False  # any required verifier can ask for a person
        suspended = False

        async def record(result: LayerResult) -> None:
            layers.append(result)
            if recorder is not None:
                await recorder(result)

        for layer in VERIFICATION_LAYERS:
            if layer == "human_review" and escalated:
                required.add("human_review")  # needs_human forces the sixth layer (D7-8')
            if short_at is not None:
                await record(
                    LayerResult(
                        layer,
                        NOT_REQUIRED if layer not in required else "SKIPPED",
                        "short-circuited",
                        {},
                    )
                )
                continue
            if layer not in required:
                if layer in removed and emptied:
                    await record(
                        LayerResult(
                            layer,
                            ERROR,
                            "ablation leaves no verification for this Task: zero layers is never a PASS",
                            {"ablated": True, "required_by_policy": True, "no_layer_left": True},
                        )
                    )
                    short_at = layer
                    continue
                if layer in removed:
                    await record(
                        LayerResult(
                            layer,
                            NOT_REQUIRED,
                            "ablated: the Task policy requires it, this run removed it",
                            {"ablated": True, "required_by_policy": True},
                        )
                    )
                    continue
                await record(
                    LayerResult(layer, NOT_REQUIRED, "not in the Task verification policy", {})
                )
                continue
            reusable = reuse is not None and layer in reuse
            if reusable and reuse is not None and document and layer == "rule_check":
                reusable = assessment_binding is not None and doc_rule_reusable(
                    reuse[layer], binding=assessment_binding
                )
            if reusable and reuse is not None:  # D7-8': resume reuses what actually passed
                result = reuse[layer]
                if layer == "critic_review":
                    critic = CriticVerdict.from_json(result.detail)
            elif layer == "format_check":
                result = format_check(envelope, client_result_id=client_result_id)
            elif layer == "rule_check":
                result = rule_check(
                    envelope,
                    task,
                    artifacts=artifacts,
                    verification_copy=verification_copy,
                    tampered=tampered,
                    knowledge=knowledge,
                    require_synthesis_knowledge=require_synthesis_knowledge,
                    extra_problems=action_problems or (),
                    local_code_execution=self._local_code_execution,
                    domain=actual_domain,
                )
                if document:
                    if assessment_binding is None or evidence_resolver is None:
                        result = LayerResult(
                            layer,
                            ERROR,
                            "document assessment binding/resolver unavailable",
                            {
                                **dict(result.detail),
                                "assessment_error": "missing_binding_or_resolver",
                            },
                        )
                    else:
                        try:
                            result = citation_integrity(
                                binding=assessment_binding,
                                envelope=envelope,
                                resolver=evidence_resolver,
                                structural_result=result,
                            )
                        except ContractError as error:
                            result = LayerResult(
                                layer, ERROR, str(error), {"assessment_error": str(error)}
                            )
            elif layer == "critic_review":
                try:
                    critic = await run_critic(test_output)
                    result = LayerResult(
                        layer,
                        PASS if critic.passed else FAIL,
                        "critic found no blocker"
                        if critic.passed
                        else "critic found a blocker: "
                        + "; ".join(
                            str(f.get("detail"))
                            for f in critic.findings
                            if f.get("severity") == "blocker"
                        ),
                        critic.to_json(),
                    )
                    if critic.passed and critic.needs_human:  # D7-8': not a PASS, not a FAIL
                        if needs_human_allowed:
                            result = LayerResult(
                                layer,
                                NEEDS_HUMAN,
                                "the Critic cannot reliably judge; a person decides",
                                critic.to_json(),
                            )
                            escalated = True
                        else:
                            result = LayerResult(
                                layer,
                                FAIL,
                                "the Critic asked for a person again; one escalation per Task",
                                critic.to_json(),
                            )
                except ContractError as error:
                    result = LayerResult(layer, ERROR, f"critic verdict unusable: {error}", {})
            elif layer == "code_test" and not self._local_code_execution:
                # host support 0.9.8: a Task from before the switch still asks for the layer;
                # it cannot run here, which is an ERROR — never a PASS or NOT_REQUIRED
                result = LayerResult(
                    layer,
                    ERROR,
                    "local_code_execution is off in this deployment: model-written tests are not run on this machine",
                    {"undeployed": True, "local_code_execution": False},
                )
            elif layer == "code_test":
                # step 3 (D3-9'): Mission-level pytest targets are judged on the
                # integrated tree, not against one Task's partial workspace
                result = await code_test(
                    task,
                    verification_copy=verification_copy,
                    timeout=self._test_timeout,
                    executor=self._executor,
                )
                runs = result.detail.get("runs", [])
                test_output = "\n".join(
                    str(r.get("stdout", "")) for r in runs if isinstance(r, Mapping)
                )
            elif layer == "human_review":  # step 7 (D7-8'): the sixth layer
                result = human_layer(human)
                suspended = result.status == SUSPENDED
            else:  # formal_check is not deployed in this build
                result = LayerResult(
                    layer, ERROR, "layer not deployed in this build", {"undeployed": True}
                )
            if result.status == NEEDS_HUMAN:
                if not needs_human_allowed and (
                    layer != "critic_review"
                    or (
                        actual_domain is not None
                        and actual_domain.id == "doc-research-v1"
                        and actual_domain.version == "3"
                    )
                ):
                    result = LayerResult(
                        layer,
                        FAIL,
                        "one human escalation per Task; quota already used",
                        {**dict(result.detail), "escalation_quota_exhausted": True},
                    )
                else:
                    escalated = True
            await record(result)
            if result.status in {FAIL, ERROR}:
                short_at = layer
        human_passed = any(done.layer == "human_review" and done.status == PASS for done in layers)
        passed = (
            short_at is None
            and not suspended
            and all(
                layer.status == PASS or (layer.status == NEEDS_HUMAN and human_passed)
                for layer in layers
                if layer.layer in required
            )
        )
        return Verdict(
            passed=passed,
            layers=tuple(layers),
            critic=critic,
            short_circuited_at=short_at,
            suspended=suspended,
        )


__all__ = ("VERIFIER_VERSION", "CriticRunner", "Verdict", "VerifierRouter")
