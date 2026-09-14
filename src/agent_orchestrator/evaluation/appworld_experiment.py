# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-side, serial AppWorld evaluations over the durable experiment matrix.

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
from datetime import UTC, datetime, timedelta
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
from .metered_provider import (
    MeteredProvider,
    ProviderIdentityMismatch,
    RunWindowDenied,
    UnknownProviderUsage,
)
from .run_window import ProviderWindowSchedule, WindowDecision, admit_run_window


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
        _validate_common_config(self)

    def identity(self) -> dict[str, Any]:
        return _config_identity(self)


@dataclass(frozen=True, slots=True)
class AppWorldMatrixConfig:
    """Frozen general matrix; task count and repetitions come from the manifest."""

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
    run_window: ProviderWindowSchedule | None = None
    queue_wait_seconds: float = 0
    drain_margin_seconds: float = 0
    max_inflight_tokens: int | None = None

    def __post_init__(self) -> None:
        _validate_common_config(self)
        if self.run_window is not None and not isinstance(self.run_window, ProviderWindowSchedule):
            raise TypeError("run_window must be ProviderWindowSchedule")
        if self.run_window is not None and self.run_window.provider_id != self.manifest.provider:
            raise ValueError("run_window provider must match manifest provider")
        for name in ("queue_wait_seconds", "drain_margin_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and nonnegative")
            object.__setattr__(self, name, float(value))
        if self.run_window is None and (self.queue_wait_seconds or self.drain_margin_seconds):
            raise ValueError("queue/drain margins require an explicit run_window")
        if self.max_inflight_tokens is not None and (
            type(self.max_inflight_tokens) is not int or self.max_inflight_tokens < 1
        ):
            raise ValueError("max_inflight_tokens must be a positive integer")

    def identity(self) -> dict[str, Any]:
        # Keep the general matrix in its own namespace; each run uses its
        # manifest-derived seed, including the repetition offset.
        identity = _config_identity(self)
        identity["schema_version"] = 2
        identity["world_seed"] = "manifest_run_seed"
        identity["run_window"] = asdict(self.run_window) if self.run_window is not None else None
        identity["queue_wait_seconds"] = self.queue_wait_seconds
        identity["drain_margin_seconds"] = self.drain_margin_seconds
        identity["max_inflight_tokens"] = self.max_inflight_tokens
        return identity


def _window_receipt(decision: WindowDecision) -> dict[str, Any]:
    return {
        "reason": decision.reason,
        "next_allowed_start": (
            decision.next_allowed_start.isoformat() if decision.next_allowed_start else None
        ),
        "window_end": decision.window_end.isoformat() if decision.window_end else None,
    }


def _config_identity(config: AppWorldPilotConfig | AppWorldMatrixConfig) -> dict[str, Any]:
    # Never persist an environment URL: it can contain authentication data.
    return {
        "schema_version": 1,
        "manifest_sha256": config.manifest.fingerprint,
        "source_fingerprint": config.source_fingerprint,
        "profile_fingerprint": config.profile_fingerprint,
        "config_fingerprint": config.config_fingerprint,
        "environment_fingerprint": config.environment_fingerprint,
        "environment_url_sha256": (
            hashlib.sha256(config.remote_environment_url.encode("utf-8")).hexdigest()
            if config.remote_environment_url is not None
            else None
        ),
        "world_seed": 100,
        "provider_timeout_seconds": config.provider_timeout_seconds,
        "budget": asdict(config.manifest.budget),
        "physical_slots": config.manifest.physical_slots,
        "default_output_tokens": config.default_output_tokens,
        "maximum_output_tokens": config.maximum_output_tokens,
        "candidate_repetitions": config.candidate_repetitions,
    }


def _validate_common_config(config: AppWorldPilotConfig | AppWorldMatrixConfig) -> None:
    if not isinstance(config.manifest, ExperimentManifest):
        raise TypeError("manifest must be ExperimentManifest")
    for name in (
        "source_fingerprint",
        "profile_fingerprint",
        "config_fingerprint",
        "environment_fingerprint",
    ):
        value = getattr(config, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty fingerprint")
    for name in ("default_output_tokens", "maximum_output_tokens", "candidate_repetitions"):
        value = getattr(config, name)
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if config.default_output_tokens > config.maximum_output_tokens:
        raise ValueError("default output cap exceeds maximum")
    if config.remote_environment_url is not None and (
        not isinstance(config.remote_environment_url, str)
        or not config.remote_environment_url.strip()
    ):
        raise ValueError("remote environment URL must be nonempty when provided")
    if (
        isinstance(config.provider_timeout_seconds, bool)
        or not isinstance(config.provider_timeout_seconds, (int, float))
        or not math.isfinite(config.provider_timeout_seconds)
        or not 0 < config.provider_timeout_seconds < config.manifest.budget.seconds
    ):
        raise ValueError("provider timeout must be positive and below episode deadline")
    object.__setattr__(config, "provider_timeout_seconds", float(config.provider_timeout_seconds))


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
    if type(config) is not AppWorldPilotConfig:
        raise TypeError("config must be AppWorldPilotConfig")
    return await _run_appworld(
        config,
        evidence_root=evidence_root,
        provider_factory=provider_factory,
        estimator_factory=estimator_factory,
        tokenizer=tokenizer,
        context_policy=context_policy,
        extra_reserve_factory=extra_reserve_factory,
        episode_factory=episode_factory,
        arm_executor=arm_executor,
        identity_filename="appworld-pilot.json",
        identity_label="AppWorld pilot",
        record_key="pilot",
    )


async def run_appworld_matrix(
    config: AppWorldMatrixConfig,
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
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run a frozen S/R/D/F matrix, resuming only never-admitted episodes."""
    if type(config) is not AppWorldMatrixConfig:
        raise TypeError("config must be AppWorldMatrixConfig")
    return await _run_appworld(
        config,
        evidence_root=evidence_root,
        provider_factory=provider_factory,
        estimator_factory=estimator_factory,
        tokenizer=tokenizer,
        context_policy=context_policy,
        extra_reserve_factory=extra_reserve_factory,
        episode_factory=episode_factory,
        arm_executor=arm_executor,
        identity_filename="appworld-matrix.json",
        identity_label="AppWorld matrix",
        record_key="matrix",
        clock=clock or _utc_clock,
    )


def _utc_clock() -> datetime:
    return datetime.now(UTC)


async def _run_appworld(
    config: AppWorldPilotConfig | AppWorldMatrixConfig,
    *,
    evidence_root: str | Path,
    provider_factory: Callable[[RunContext, float], Provider],
    estimator_factory: Callable[[Provider], Callable[[ProviderRequest], int]],
    tokenizer: Any,
    context_policy: Any,
    extra_reserve_factory: Callable[[Provider], Callable[[ProviderRequest], int] | None] | None,
    episode_factory: Callable[[AppWorldConfig], AppWorldEpisode],
    arm_executor: Callable[[str, AppWorldEpisode, ArmRuntime, Path], Awaitable[dict[str, Any]]],
    identity_filename: str,
    identity_label: str,
    record_key: str,
    clock: Callable[[], datetime] = _utc_clock,
) -> dict[str, Any]:
    root = Path(evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / "experiment.json"
    identity_path = root / identity_filename
    with _exclusive(root / "experiment.lock"):
        identity = config.identity()
        if identity_path.exists():
            if _json(json.loads(identity_path.read_text(encoding="utf-8"))) != _json(identity):
                raise ValueError(f"{identity_label} identity mismatch")
            if record_key == "matrix" and not receipt_path.exists():
                raise ValueError("AppWorld matrix receipt missing; cannot safely resume")
        else:
            if receipt_path.exists():
                raise ValueError(f"existing matrix has no {identity_label} identity")
            _write(identity_path, identity)
        existing_receipt = receipt_path.exists()
        if record_key == "matrix" and existing_receipt:
            previous = json.loads(receipt_path.read_text(encoding="utf-8"))
            if _json(previous.get("matrix_identity")) != _json(identity):
                raise ValueError("matrix receipt source/profile/config identity mismatch")
        document = _load(receipt_path, config.manifest)
        if record_key == "matrix":
            document["matrix_identity"] = identity
        if any(record.get(record_key, {}).get("stop_matrix") for record in document["runs"]):
            return json.loads(_json(document))

        def persist() -> None:
            _write(receipt_path, document)

        for run, record in zip(config.manifest.runs(), document["runs"], strict=True):
            if record["status"] != "pending":
                continue
            if isinstance(config, AppWorldMatrixConfig) and config.run_window is not None:
                decision = admit_run_window(
                    config.run_window,
                    provider_id=config.manifest.provider,
                    now=clock(),
                    max_duration=timedelta(seconds=config.manifest.budget.seconds),
                    queue_wait=timedelta(seconds=config.queue_wait_seconds),
                    drain_margin=timedelta(seconds=config.drain_margin_seconds),
                )
                if not decision.admitted:
                    record["run_window"] = _window_receipt(decision)
                    persist()
                    break
                record.pop("run_window", None)

            async def execute(context: RunContext) -> ExecutionResult:
                meter: MeteredProvider | None = None
                episode: AppWorldEpisode | None = None
                outcome: dict[str, Any] | None = None
                official: dict[str, Any] | None = None
                error: BaseException | None = None
                finalize_error: BaseException | None = None
                phase = "provider_setup"
                unknown_event = asyncio.Event()
                handoff_denial: WindowDecision | None = None

                def before_handoff(queued_seconds: float) -> None:
                    nonlocal handoff_denial
                    assert isinstance(config, AppWorldMatrixConfig)
                    assert config.run_window is not None
                    decision = admit_run_window(
                        config.run_window,
                        provider_id=config.manifest.provider,
                        now=clock(),
                        max_duration=timedelta(seconds=config.provider_timeout_seconds),
                        queue_wait=timedelta(
                            seconds=max(0.0, config.queue_wait_seconds - queued_seconds)
                        ),
                        drain_margin=timedelta(seconds=config.drain_margin_seconds),
                    )
                    if not decision.admitted:
                        handoff_denial = decision
                        raise RunWindowDenied("physical handoff outside frozen run window")

                def report(counters: Any) -> None:
                    if meter is not None:
                        record["admission_denials"] = list(meter.admission_denials)
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
                        before_handoff=(
                            before_handoff
                            if isinstance(config, AppWorldMatrixConfig)
                            and config.run_window is not None
                            else None
                        ),
                        max_inflight_tokens=(
                            config.max_inflight_tokens
                            if isinstance(config, AppWorldMatrixConfig)
                            else None
                        ),
                    )
                    phase = "world_setup"
                    episode = episode_factory(
                        AppWorldConfig(
                            context.run.task_id,
                            context.run.run_id,  # Unique output, never reused across arms.
                            remote_environment_url=config.remote_environment_url,
                            random_seed=(100 if record_key == "pilot" else context.run.seed),
                        )
                    )
                    phase = "arm_execution"
                    executor_id = next(
                        arm.executor_id for arm in config.manifest.arms
                        if arm.arm == context.run.arm
                    )
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
                        knowledge_protocol=(
                            executor_id
                            if "-host-public-knowledge-" in executor_id
                            else None
                        ),
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
                runtime_success = _runtime_success(outcome)
                valid_success = (
                    error is None
                    and finalize_error is None
                    and not fatal
                    and runtime_success is True
                    and official_success is True
                    and handoff_denial is None
                    and not (meter is not None and meter.admission_denials)
                )
                record[record_key] = {
                    "executor_returned": error is None,
                    "runtime_success": runtime_success,
                    "runtime_error": type(error).__name__ if error is not None else None,
                    "finalization_error": (
                        type(finalize_error).__name__ if finalize_error is not None else None
                    ),
                    "official_success": official_success,
                    "valid_success": valid_success,
                    "official_result": official,
                    "arm_result": outcome,
                    "usage": {
                        "known_counters": asdict(meter.counters) if meter is not None else None,
                        "unknown_usage_calls": unknown,
                        "actual_tokens_complete": unknown == 0 if unknown is not None else None,
                    },
                    "provider_calls": list(meter.observations) if meter is not None else [],
                    "admission_denials": list(meter.admission_denials) if meter is not None else [],
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
                if handoff_denial is not None:
                    record["run_window"] = _window_receipt(handoff_denial)
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
                    valid_success,
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
            if record.get(record_key, {}).get("stop_matrix"):
                break
            if "run_window" in record:
                for pending in document["runs"]:
                    if pending["status"] == "pending":
                        pending["run_window"] = record["run_window"]
                        persist()
                        break
                break
        return json.loads(_json(document))


__all__ = (
    "AppWorldMatrixConfig",
    "AppWorldPilotConfig",
    "run_appworld_matrix",
    "run_appworld_pilot",
)
