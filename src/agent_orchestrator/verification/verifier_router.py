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
from ..memory.verified_knowledge import KnowledgeIndex
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

CriticRunner = Callable[[str | None], Awaitable[CriticVerdict]]


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    layers: tuple[LayerResult, ...]
    critic: CriticVerdict | None
    short_circuited_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "layers": [layer.to_json() for layer in self.layers],
            "critic": None if self.critic is None else self.critic.to_json(),
            "short_circuited_at": self.short_circuited_at,
        }

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [layer.to_json() for layer in self.layers if layer.status in {FAIL, ERROR}]


class VerifierRouter:
    def __init__(self, *, test_timeout: float = 120.0) -> None:
        self._test_timeout = test_timeout

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
    ) -> Verdict:
        required = set(task.verification_policy)
        layers: list[LayerResult] = []
        critic: CriticVerdict | None = None
        short_at: str | None = None
        test_output: str | None = None

        async def record(result: LayerResult) -> None:
            layers.append(result)
            if recorder is not None:
                await recorder(result)

        for layer in VERIFICATION_LAYERS:
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
                await record(
                    LayerResult(layer, NOT_REQUIRED, "not in the Task verification policy", {})
                )
                continue
            if layer == "format_check":
                result = format_check(envelope, client_result_id=client_result_id)
            elif layer == "rule_check":
                result = rule_check(
                    envelope,
                    task,
                    artifacts=artifacts,
                    verification_copy=verification_copy,
                    tampered=tampered,
                    knowledge=knowledge,
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
                except ContractError as error:
                    result = LayerResult(layer, ERROR, f"critic verdict unusable: {error}", {})
            elif layer == "code_test":
                # step 3 (D3-9'): Mission-level pytest targets are judged on the
                # integrated tree, not against one Task's partial workspace
                result = await code_test(
                    task,
                    verification_copy=verification_copy,
                    timeout=self._test_timeout,
                )
                runs = result.detail.get("runs", [])
                test_output = "\n".join(
                    str(r.get("stdout", "")) for r in runs if isinstance(r, Mapping)
                )
            else:  # formal_check / human_review are not deployed in this build
                result = LayerResult(layer, ERROR, "layer not deployed in this build", {})
            await record(result)
            if result.status in {FAIL, ERROR}:
                short_at = layer
        passed = short_at is None and all(
            layer.status == PASS for layer in layers if layer.layer in required
        )
        return Verdict(
            passed=passed, layers=tuple(layers), critic=critic, short_circuited_at=short_at
        )


__all__ = ("CriticRunner", "Verdict", "VerifierRouter")
