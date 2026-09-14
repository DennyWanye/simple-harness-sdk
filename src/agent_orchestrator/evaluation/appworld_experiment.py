# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-side, serial AppWorld pilot over the durable experiment matrix.

The executor sees the agent-facing episode and one shared physical meter. The
host scores only after execution has stopped. This coordinator uses the matrix
runner's locked receipt and per-run admission primitives so a fatal episode
can leave the rest of the matrix pending; run_experiment itself always continues.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from simple_harness.providers import Provider, ProviderRequest

from .appworld import AppWorldConfig, AppWorldEpisode
from .appworld_arms import ArmRuntime, execute_arm
from .experiment import (
    ARMS,
    ArmExecutor,
    ExecutionResult,
    ExperimentManifest,
    RunContext,
    _exclusive,
    _execute_one,
    _json,
    _load,
    _write,
)
from .metered_provider import MeteredProvider, ProviderIdentityMismatch, UnknownProviderUsage


def _runtime_success(outcome: dict[str, Any] | None) -> bool | None:
    """Returning from the driver is distinct from its Agent/Mission completing."""
    if outcome is None:
        return None
    if "mission_status" in outcome:
        return outcome["mission_status"] == "COMPLETED"
    if "runtime_states" in outcome:
        states = outcome["runtime_states"]
        return bool(states) and states[-1] == "committed"
    return outcome.get("completed") if type(outcome.get("completed")) is bool else None


@dataclass(frozen=True, slots=True)
class AppWorldPilotConfig:
    manifest: ExperimentManifest
    source_fingerprint: str
    profile_fingerprint: str
    config_fingerprint: str
    environment_fingerprint: str
    provider_timeout_seconds: float
    remote_environment_url: str | None = None
    default_output_tokens: int = 8192
    maximum_output_tokens: int = 32768
    candidate_repetitions: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ExperimentManifest):
            raise TypeError("manifest must be ExperimentManifest")
        if len(self.manifest.task_ids) != 4 or self.manifest.repetitions != 1:
            raise ValueError("pilot requires four distinct tasks and one run per arm")
        if self.manifest.seed != 100 or tuple(a.arm for a in self.manifest.arms) != ARMS:
            raise ValueError("pilot requires fixed seed 100 and S/R/D/F arms")
        for name in (
            "source_fingerprint",
            "profile_fingerprint",
            "config_fingerprint",
            "environment_fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty fingerprint")
        for name in ("default_output_tokens", "maximum_output_tokens", "candidate_repetitions"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.default_output_tokens > self.maximum_output_tokens:
            raise ValueError("default output cap exceeds maximum")
        if self.remote_environment_url is not None and (
            not isinstance(self.remote_environment_url, str)
            or not self.remote_environment_url.strip()
        ):
            raise ValueError("remote environment URL must be nonempty when provided")
        if (
            isinstance(self.provider_timeout_seconds, bool)
            or not isinstance(self.provider_timeout_seconds, (int, float))
            or not math.isfinite(self.provider_timeout_seconds)
            or not 0 < self.provider_timeout_seconds < self.manifest.budget.seconds
        ):
            raise ValueError("provider timeout must be positive and below episode deadline")
        # Normalise identity so 600 and 600.0 have the same frozen value.
        object.__setattr__(self, "provider_timeout_seconds", float(self.provider_timeout_seconds))

    def identity(self) -> dict[str, Any]:
        # Never persist an environment URL: it can contain authentication data.
        return {
            "schema_version": 1,
            "manifest_sha256": self.manifest.fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "environment_fingerprint": self.environment_fingerprint,
            "environment_url_sha256": (
                hashlib.sha256(self.remote_environment_url.encode("utf-8")).hexdigest()
                if self.remote_environment_url is not None
                else None
            ),
            "world_seed": 100,
            "provider_timeout_seconds": self.provider_timeout_seconds,
            "budget": asdict(self.manifest.budget),
            "physical_slots": self.manifest.physical_slots,
            "default_output_tokens": self.default_output_tokens,
            "maximum_output_tokens": self.maximum_output_tokens,
            "candidate_repetitions": self.candidate_repetitions,
        }


async def run_appworld_pilot(
    config: AppWorldPilotConfig,
    *,
    evidence_root: str | Path,
    provider_factory: Callable[[RunContext, float], Provider],
    estimator_factory: Callable[[Provider], Callable[[ProviderRequest], int]],
    tokenizer: Any,
    context_policy: Any,
    extra_reserve_factory: Callable[[Provider], Callable[[ProviderRequest], int] | None]
    | None = None,
    episode_factory: Callable[[AppWorldConfig], AppWorldEpisode] = AppWorldEpisode,
    arm_executor: Callable[
        [str, AppWorldEpisode, ArmRuntime, Path], Awaitable[dict[str, Any]]
    ] = execute_arm,
) -> dict[str, Any]:
    """Run the fixed 16-episode pilot, resuming only pending matrix identities.

    Factories must be bound to the fingerprinted source/profile/config. The
    caller binds the real provider to the supplied frozen timeout and its
    serializer-bound estimator; no
    model or world is constructed during admission/resume validation.
    """
    root = Path(evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / "experiment.json"
    identity_path = root / "appworld-pilot.json"
    with _exclusive(root / "experiment.lock"):
        identity = config.identity()
        if identity_path.exists():
            if _json(json.loads(identity_path.read_text(encoding="utf-8"))) != _json(identity):
                raise ValueError("AppWorld pilot identity mismatch")
        else:
            if receipt_path.exists():
                raise ValueError("existing matrix has no AppWorld pilot identity")
            _write(identity_path, identity)
        document = _load(receipt_path, config.manifest)
        if any(record.get("pilot", {}).get("stop_matrix") for record in document["runs"]):
            return json.loads(_json(document))

        def persist() -> None:
            _write(receipt_path, document)

        for run, record in zip(config.manifest.runs(), document["runs"], strict=True):
            if record["status"] != "pending":
                continue

            async def execute(context: RunContext) -> ExecutionResult:
                meter: MeteredProvider | None = None
                episode: AppWorldEpisode | None = None
                outcome: dict[str, Any] | None = None
                official: dict[str, Any] | None = None
                error: BaseException | None = None
                finalize_error: BaseException | None = None
                phase = "provider_setup"
                unknown_event = asyncio.Event()

                def report(counters: Any) -> None:
                    context.report_usage(counters)
                    if meter is not None and meter.unknown_usage_calls:
                        unknown_event.set()

                try:
                    provider = provider_factory(context, config.provider_timeout_seconds)
                    meter = MeteredProvider(
                        provider,
                        RunContext(context.manifest, context.run, report),
                        estimate_input_tokens=estimator_factory(provider),
                        extra_input_reserve=(
                            extra_reserve_factory(provider) if extra_reserve_factory else None
                        ),
                    )
                    phase = "world_setup"
                    episode = episode_factory(
                        AppWorldConfig(
                            context.run.task_id,
                            context.run.run_id,  # Unique output, never reused across arms.
                            remote_environment_url=config.remote_environment_url,
                            random_seed=100,
                        )
                    )
                    phase = "arm_execution"
                    runtime = ArmRuntime(
                        meter,
                        config.manifest.model,
                        tokenizer,
                        context_policy,
                        config.manifest.budget,
                        physical_slots=config.manifest.physical_slots,
                        default_output_tokens=config.default_output_tokens,
                        maximum_output_tokens=config.maximum_output_tokens,
                        repetitions=config.candidate_repetitions,
                    )
                    arm_task = asyncio.ensure_future(
                        arm_executor(context.run.arm, episode, runtime, root / context.run.run_id)
                    )
                    watcher = asyncio.create_task(unknown_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (arm_task, watcher), return_when=asyncio.FIRST_COMPLETED
                        )
                        if watcher in done and not arm_task.done():
                            arm_task.cancel()
                        try:
                            outcome = await arm_task
                        except asyncio.CancelledError:
                            current = asyncio.current_task()
                            if (
                                meter.unknown_usage_calls
                                and current is not None
                                and not current.cancelling()
                            ):
                                raise UnknownProviderUsage(
                                    "physical usage unknown; arm stopped"
                                ) from None
                            raise
                    finally:
                        watcher.cancel()
                        if not arm_task.done():
                            arm_task.cancel()
                            try:
                                await arm_task  # The arm must stop before host scoring.
                            except asyncio.CancelledError:
                                pass
                    if not isinstance(outcome, dict):
                        raise TypeError("arm executor must return a dictionary")
                except BaseException as exc:
                    error = exc
                finally:
                    if episode is not None:
                        # Synchronous host finalization runs even under task cancellation.
                        # Never pass the official score back into the arm executor.
                        try:
                            official = episode.finalize()
                        except BaseException as exc:
                            finalize_error = exc

                unknown = meter.unknown_usage_calls if meter is not None else None
                fatal = (
                    phase != "arm_execution"
                    or finalize_error is not None
                    or isinstance(error, (UnknownProviderUsage, ProviderIdentityMismatch, OSError))
                    or (unknown is not None and unknown > 0)
                )
                official_success = official.get("success") if isinstance(official, dict) else None
                if type(official_success) is not bool:
                    official_success = None
                record["pilot"] = {
                    "executor_returned": error is None,
                    "runtime_success": _runtime_success(outcome),
                    "runtime_error": type(error).__name__ if error is not None else None,
                    "finalization_error": (
                        type(finalize_error).__name__ if finalize_error is not None else None
                    ),
                    "official_success": official_success,
                    "official_result": official,
                    "arm_result": outcome,
                    "usage": {
                        "known_counters": asdict(meter.counters) if meter is not None else None,
                        "unknown_usage_calls": unknown,
                        "actual_tokens_complete": unknown == 0 if unknown is not None else None,
                    },
                    "provider_calls": list(meter.observations) if meter is not None else [],
                    "declared_task_status": (
                        official.get("declared_task_status") if isinstance(official, dict) else None
                    ),
                    "false_completion": (
                        official.get("declared_task_status") == "success"
                        and official_success is False
                        if isinstance(official, dict)
                        and official.get("declared_task_status") in {"success", "fail", "pending"}
                        else None
                    ),
                    "stop_matrix": fatal,
                }
                persist()
                if isinstance(error, asyncio.CancelledError):
                    raise error
                if isinstance(finalize_error, asyncio.CancelledError):
                    raise finalize_error
                if meter is None:
                    raise error or finalize_error or RuntimeError("provider setup failed")
                # The runner receives the settled lower bound even on failure;
                # unknown use stays explicit in the pilot receipt and stops admission.
                return ExecutionResult(
                    error is None
                    and finalize_error is None
                    and not fatal
                    and _runtime_success(outcome) is not False,
                    meter.counters,
                )

            arm = next(arm for arm in config.manifest.arms if arm.arm == run.arm)
            bound = ArmExecutor(
                run.arm, arm.executor_id, config.manifest.provider, config.manifest.model, execute
            )
            await _execute_one(config.manifest, run, bound, record, persist)
            # A runner deadline/cancellation with an unknown physical call must
            # also block the remaining matrix. A deadline without that evidence
            # is a terminal per-run result, with subsequent runs still pending.
            if record.get("pilot", {}).get("stop_matrix"):
                break
        return json.loads(_json(document))


__all__ = ("AppWorldPilotConfig", "run_appworld_pilot")
