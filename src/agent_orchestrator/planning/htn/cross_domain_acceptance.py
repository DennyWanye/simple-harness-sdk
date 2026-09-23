"""Deterministic H8 cross-domain scenario matrix (no provider integration)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...contracts.models import ContractError
from ...contracts.semantic_base import content_hash_of


class ScenarioKind(StrEnum):
    NORMAL = "normal"
    REPAIR = "repair"
    EVIDENCE_CONFLICT = "evidence-conflict"
    RECOVERY = "recovery"


class FourArm(StrEnum):
    STRONG_SINGLE_AGENT = "strong-single-agent"
    H_NATIVE = "h-native"
    H_NATIVE_NO_REPAIR = "h-native-no-repair"
    H_SOLVER = "h-solver"


@dataclass(frozen=True, slots=True)
class CrossDomainScenario:
    scenario_id: str
    domain: str
    kind: ScenarioKind


@dataclass(frozen=True, slots=True)
class AcceptanceResultV1:
    scenario_id: str
    arm: FourArm
    trial: int
    passed: bool


@dataclass(frozen=True, slots=True)
class CrossDomainAcceptancePlan:
    domains: tuple[str, ...]
    scenarios: tuple[CrossDomainScenario, ...]
    trials: int
    arms: tuple[FourArm, ...]
    model_id: str
    toolset_hash: str
    total_budget: int
    config_hash: str

    @classmethod
    def build(
        cls,
        domains: Sequence[str] = ("code-v1", "appworld-v1", "drone-sim-v1"),
        *,
        trials: int = 3,
        model_id: str = "gpt-5.6-sol",
        toolset_hash: str = "tools-v1",
        total_budget: int = 1000,
    ) -> CrossDomainAcceptancePlan:
        ordered = tuple(sorted(str(item) for item in domains))
        if ordered != ("appworld-v1", "code-v1", "drone-sim-v1"):
            raise ContractError("H8 requires code, appworld and drone-sim domains")
        if trials < 3 or total_budget < 1 or not model_id.strip() or not toolset_hash.strip():
            raise ContractError("H8 requires at least three trials and a frozen runtime config")
        scenarios: list[CrossDomainScenario] = []
        for domain in ordered:
            for kind in ScenarioKind:
                count = {
                    ScenarioKind.NORMAL: 8,
                    ScenarioKind.REPAIR: 4,
                    ScenarioKind.EVIDENCE_CONFLICT: 2,
                    ScenarioKind.RECOVERY: 2,
                }[kind]
                for index in range(count):
                    scenarios.append(
                        CrossDomainScenario(f"{domain}:{kind.value}:{index + 1}", domain, kind)
                    )
        config = {"model_id": model_id, "toolset_hash": toolset_hash, "total_budget": total_budget}
        return cls(
            ordered,
            tuple(scenarios),
            trials,
            tuple(FourArm),
            model_id,
            toolset_hash,
            total_budget,
            content_hash_of(config),
        )

    def validate_run_config(self, config: Mapping[str, Any]) -> bool:
        return (
            config.get("model_id") == self.model_id
            and config.get("toolset_hash") == self.toolset_hash
            and config.get("total_budget") == self.total_budget
        )

    def run(
        self,
        evaluator: Callable[[CrossDomainScenario, FourArm, int], bool],
        *,
        config: Mapping[str, Any],
    ) -> tuple[AcceptanceResultV1, ...]:
        """Execute the frozen matrix through a caller-supplied evaluator."""
        if not self.validate_run_config(config):
            raise ContractError("acceptance run config differs from frozen plan")
        return tuple(
            AcceptanceResultV1(
                scenario.scenario_id,
                arm,
                trial,
                bool(evaluator(scenario, arm, trial)),
            )
            for scenario in self.scenarios
            for arm in self.arms
            for trial in range(1, self.trials + 1)
        )

    def validate_results(self, results: Sequence[AcceptanceResultV1]) -> None:
        expected = {
            (scenario.scenario_id, arm, trial)
            for scenario in self.scenarios
            for arm in self.arms
            for trial in range(1, self.trials + 1)
        }
        actual = {(item.scenario_id, item.arm, item.trial) for item in results}
        if len(actual) != len(results):
            raise ContractError("acceptance results contain duplicate scenario/arm/trial")
        if actual != expected:
            raise ContractError("acceptance results do not cover the frozen matrix")


__all__ = (
    "AcceptanceResultV1",
    "CrossDomainAcceptancePlan",
    "CrossDomainScenario",
    "FourArm",
    "ScenarioKind",
)
