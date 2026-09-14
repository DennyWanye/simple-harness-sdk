# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One AppWorld task world per episode, with evaluation kept on the host side.

The agent receives only ``episode.agent``. The host owns the episode and calls
``finalize`` after the agent stops, including on failure or timeout. Using the
episode as a context manager makes that cleanup unconditional.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .appworld_api_observations import (
    PUBLIC_READ_APIS,
    AppWorldAPILedger,
    AppWorldAPIReceipt,
    canonical,
    project,
)
from .appworld_observations import AppWorldExecutionReceipt, AppWorldObservationLedger

# Unified AppWorld keeps task time and server state process-wide. Only one
# episode may own that state; parallel episodes belong in separate processes.
_ACTIVE_WORLD = Lock()


@dataclass(frozen=True, slots=True)
class AppWorldConfig:
    task_id: str
    experiment_name: str
    remote_environment_url: str | None = None
    random_seed: int = 100

    def __post_init__(self) -> None:
        if type(self.random_seed) is not int or self.random_seed < 0:
            raise ValueError("random_seed must be a nonnegative integer")
        for name in ("task_id", "experiment_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")


def _default_world_factory(
    *,
    task_id: str,
    experiment_name: str,
    remote_environment_url: str | None = None,
    random_seed: int = 100,
) -> Any:
    # AppWorld is an optional dependency, loaded only when an episode starts.
    AppWorld = importlib.import_module("appworld").AppWorld

    if remote_environment_url is None:
        raise ValueError("A separate AppWorld environment service is required for real episodes")
    world = AppWorld(
        task_id=task_id,
        experiment_name=experiment_name,
        remote_environment_url=remote_environment_url,
        load_ground_truth=False,
        timeout_seconds=30,
        random_seed=random_seed,
    )
    return _ExternallyScoredWorld(world, task_id, experiment_name)


class _ExternallyScoredWorld:
    """Keep hidden checks out of the executing world; score saved output after stop."""

    def __init__(self, world: Any, task_id: str, experiment_name: str) -> None:
        self._world = world
        self._task_id = task_id
        self._experiment_name = experiment_name
        self.task = world.task
        self._save_number = 0

    def execute(self, code: str) -> str:
        return self._world.execute(code)

    def observe_public_api(self, app: str, api: str
                           ) -> tuple[dict[str, str | None], str]:
        """Independent service Requester, never the agent's Python shell."""
        if (app, api) not in PUBLIC_READ_APIS:
            raise ValueError("API is not authorized for observation")
        request = Request(
            self._world.remote_environment_url.rstrip("/") + "/host_public_observation",
            data=canonical({"task_id": self._task_id, "experiment_name": self._experiment_name,
                            "app": app, "api": api, "parameters": {}}),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=30) as stream:
            payload = stream.read(65537)
        if len(payload) > 65536:
            raise ValueError("public observation response exceeds limit")
        envelope = json.loads(payload)
        if (not isinstance(envelope, dict)
                or set(envelope) != {"task_id", "experiment_name", "app", "api",
                                     "response", "identity"}
                or envelope["task_id"] != self._task_id
                or envelope["experiment_name"] != self._experiment_name
                or envelope["app"] != app or envelope["api"] != api
                or not isinstance(envelope["identity"], str)
                or not envelope["identity"]):
            raise ValueError("public observation world identity differs")
        return project(app, api, envelope["response"]), envelope["identity"]

    def public_observation_identity(self) -> str:
        query = urlencode({"task_id": self._task_id,
                           "experiment_name": self._experiment_name})
        url = (self._world.remote_environment_url.rstrip("/")
               + "/host_observation_identity?" + query)
        with urlopen(url, timeout=5) as stream:
            payload = stream.read(1025)
        if len(payload) > 1024:
            raise ValueError("observation identity response exceeds limit")
        envelope = json.loads(payload)
        if (not isinstance(envelope, dict) or set(envelope) != {"identity"}
                or not isinstance(envelope["identity"], str)):
            raise ValueError("invalid observation identity")
        return envelope["identity"]

    def save_state(self) -> None:
        # The published remote API requires a non-null state_id although the
        # Python signature permits None. Keep checkpoint names episode-local.
        self._save_number += 1
        self._world.save_state(state_id=f"orchestrator-{self._save_number}")

    def checkpoint(self, name: str) -> None:
        self._world.save_state(state_id=name)

    def restore(self, name: str) -> None:
        self._world.load_state(state_id=name)
        # load_state replaces in-memory DBs; execute flushes the restored DBs to
        # the official output location consumed by the independent evaluator.
        self._world.execute("pass")

    def evaluate(self) -> Any:
        path_store = importlib.import_module("appworld.common.path_store").path_store

        # Evaluation also freezes simulated time. Keep it out of the process
        # hosting asyncio, provider counters and HuggingFace lazy modules.
        completed = subprocess.run(
            [sys.executable, "-m", "agent_orchestrator.evaluation.appworld_score"],
            input=json.dumps(
                {
                    "task_id": self._task_id,
                    "experiment_name": self._experiment_name,
                    "root": str(path_store.root),
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return _Score(json.loads(completed.stdout))

    def close(self) -> None:
        self._world.close()


class _Score:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def to_dict(self) -> dict[str, Any]:
        return self._result


class AppWorldAgentView:
    """Agent-visible instruction and code execution; no world or evaluator API."""

    __slots__ = ("instruction", "__execute")

    def __init__(self, instruction: str, execute: Callable[[str], dict[str, str]]) -> None:
        self.instruction = instruction
        self.__execute = execute

    def execute(self, code: str) -> dict[str, str]:
        """Return only the output that AppWorld's interactive shell shows the agent."""
        return self.__execute(code)


class AppWorldEpisode:
    """Host-owned lifecycle for one task; a factory must create a fresh world.

    Each run needs a unique experiment_name/task_id output pair. The host must
    use separate processes for parallel worlds, as required by unified AppWorld.
    """

    def __init__(
        self,
        config: AppWorldConfig,
        *,
        world_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._lock = RLock()
        self._finalized = False
        self._result: dict[str, Any] | None = None
        self._checkpoints: set[str] = set()
        self._world_change_listeners: list[Callable[[int], None]] = []
        self._observations = AppWorldObservationLedger(
            run_id=config.experiment_name, task_id=config.task_id
        )
        self._api_observations = AppWorldAPILedger(
            run_id=config.experiment_name, task_id=config.task_id,
            episode_id=self._observations.episode_id,
        )
        factory = _default_world_factory if world_factory is None else world_factory
        if not _ACTIVE_WORLD.acquire(blocking=False):
            raise RuntimeError("another AppWorld episode is active in this process")
        try:
            kwargs: dict[str, Any] = {
                "task_id": config.task_id,
                "experiment_name": config.experiment_name,
            }
            if world_factory is None:
                kwargs["random_seed"] = config.random_seed
            if config.remote_environment_url is not None:
                kwargs["remote_environment_url"] = config.remote_environment_url
            self._world = factory(**kwargs)
            try:
                instruction = self._world.task.instruction
                if not isinstance(instruction, str):
                    raise TypeError("AppWorld task instruction must be a string")
                self.agent = AppWorldAgentView(instruction, self._execute)
            except BaseException:
                self._world.close()
                raise
        except BaseException:
            _ACTIVE_WORLD.release()
            raise

    def _execute(self, code: str) -> dict[str, str]:
        if not isinstance(code, str):
            raise TypeError("AppWorld code must be a string")
        with self._lock:
            if self._finalized:
                raise RuntimeError("AppWorld episode is finalized")
            self._advance_world()
            execution_id = self._observations.new_execution_id()
            output: str | None = None
            execution_error: BaseException | None = None
            try:
                output = self._world.execute(code)
                if not isinstance(output, str):
                    raise TypeError("AppWorld execution output must be a string")
            except BaseException as error:
                execution_error = error
                output = None
            try:
                # execute normally saves itself; an explicit save also covers
                # failed execution and alternate injected world transports.
                self._save()
            except BaseException:
                self._observations.record(
                    execution_id=execution_id,
                    code=code,
                    output=output,
                    execution_succeeded=execution_error is None,
                    save_succeeded=False,
                )
                raise
            self._observations.record(
                execution_id=execution_id,
                code=code,
                output=output,
                execution_succeeded=execution_error is None,
                save_succeeded=True,
            )
            if execution_error is not None:
                raise execution_error
            assert output is not None
            return {"output": output}

    @property
    def run_id(self) -> str:
        return self._observations.run_id

    @property
    def episode_id(self) -> str:
        return self._observations.episode_id

    @property
    def world_version(self) -> int:
        with self._lock:
            return self._observations.world_version

    def on_world_change(self, listener: Callable[[int], None]) -> None:
        """Host hook; revoke dependent facts before a world mutation begins."""
        with self._lock:
            if self._finalized:
                raise RuntimeError("AppWorld episode is finalized")
            self._world_change_listeners.append(listener)

    def off_world_change(self, listener: Callable[[int], None]) -> None:
        with self._lock:
            self._world_change_listeners.remove(listener)

    def _advance_world(self) -> None:
        next_version = self._observations.world_version + 1
        for listener in tuple(self._world_change_listeners):
            listener(next_version)
        self._observations.advance_world()

    @contextmanager
    def current_api_observation(
        self, receipt: AppWorldAPIReceipt, *, app: str, api: str, response: Any
    ) -> Iterator[None]:
        """Keep episode mutations out of a Host observation's commit window."""
        with self._lock:
            if not self.verify_api_observation(
                receipt, run_id=self.run_id, episode_id=self.episode_id,
                task_id=self.config.task_id, world_version=self.world_version,
                app=app, api=api, response=response,
            ):
                raise ValueError("AppWorld API observation is not current")
            yield

    def list_execution_receipts(
        self, *, current_only: bool = False
    ) -> tuple[AppWorldExecutionReceipt, ...]:
        """Host audit view; current_only filters by the latest world generation."""
        with self._lock:
            return self._observations.list_receipts(current_only=current_only)

    def get_execution_receipt(self, execution_id: str) -> AppWorldExecutionReceipt | None:
        """Host lookup; the returned immutable receipt is detached from the ledger."""
        with self._lock:
            return self._observations.get_receipt(execution_id)

    def verify_execution_observation(
        self,
        receipt: AppWorldExecutionReceipt,
        *,
        run_id: str,
        episode_id: str,
        task_id: str,
        execution_id: str,
        code: str,
        output: str,
        world_version: int,
    ) -> bool:
        """Check current execution provenance only; output claims remain untrusted."""
        with self._lock:
            return self._observations.verify(
                receipt,
                run_id=run_id,
                episode_id=episode_id,
                task_id=task_id,
                execution_id=execution_id,
                code=code,
                output=output,
                world_version=world_version,
            )

    def observe_public_api(self, app: str, api: str, *, parameters: dict[str, Any] | None = None
                           ) -> tuple[AppWorldAPIReceipt, dict[str, str | None]]:
        """Host-only current-world GET; return a registered receipt and safe projection."""
        with self._lock:
            if self._finalized:
                raise RuntimeError("AppWorld episode is finalized")
            if (app, api) not in PUBLIC_READ_APIS or parameters not in (None, {}):
                raise ValueError("API or parameters not authorized for observation")
            observation = self._world.observe_public_api(app, api)
            if isinstance(self._world, _ExternallyScoredWorld):
                response, identity = observation
                if identity != self._world.public_observation_identity():
                    raise RuntimeError("active AppWorld service changed during observation")
            else:
                response, identity = observation, None
            response = project(app, api, response)
            receipt = self._api_observations.record(
                world_version=self._observations.world_version, app=app, api=api,
                response=response, service_identity=identity,
            )
            return receipt, response

    def list_api_observations(self, *, current_only: bool = False
                              ) -> tuple[AppWorldAPIReceipt, ...]:
        with self._lock:
            return self._api_observations.list_receipts(
                world_version=self._observations.world_version if current_only else None
            )

    def verify_api_observation(self, receipt: AppWorldAPIReceipt, *, run_id: str,
                               episode_id: str, task_id: str, world_version: int,
                               app: str, api: str, response: Any,
                               blocking: bool = True) -> bool:
        # Core acceptance may already own Store's transaction lock. A concurrent
        # mutation owns the episode lock and revokes in Store; never invert them.
        if not self._lock.acquire(blocking=blocking):
            return False
        try:
            if self._finalized:
                return False
            try:
                identity = (self._world.public_observation_identity()
                            if isinstance(self._world, _ExternallyScoredWorld) else None)
            except (OSError, ValueError, RuntimeError, KeyError, TimeoutError):
                return False
            return (self._api_observations.verify(
                receipt, run_id=run_id, task_id=task_id, episode_id=episode_id,
                world_version=world_version, app=app, api=api, response=response,
                service_identity=identity,
            ) and world_version == self._observations.world_version)
        finally:
            self._lock.release()

    def _save(self) -> None:
        # Published PyPI 0.1.3.post1 exposes save_state; newer upstream also has
        # save. Both persist the current world, and missing support is an error.
        save = getattr(self._world, "save", None)
        if save is None:
            save = self._world.save_state
        save()

    def checkpoint(self) -> str:
        """Host-only candidate snapshot; never exposed through the agent gateway."""
        with self._lock:
            if self._finalized:
                raise RuntimeError("AppWorld episode is finalized")
            name = f"candidate-{len(self._checkpoints)}"
            self._world.checkpoint(name)
            self._checkpoints.add(name)
            return name

    def restore(self, checkpoint: str) -> None:
        """Restore a candidate from this episode without observing hidden scores."""
        with self._lock:
            if self._finalized:
                raise RuntimeError("AppWorld episode is finalized")
            if checkpoint not in self._checkpoints:
                raise ValueError("unknown episode checkpoint")
            self._advance_world()
            self._world.restore(checkpoint)

    def finalize(self) -> dict[str, Any]:
        """Host-only final save, official evaluation, and close; call after agent stops."""
        with self._lock:
            if self._finalized:
                if self._result is None:
                    raise RuntimeError("AppWorld episode was finalized without a result")
                return dict(self._result)
            self._finalized = True
            try:
                # A closed episode cannot lend its public facts to later Tasks.
                self._advance_world()
                try:
                    self._save()
                except BaseException:
                    self._advance_world()
                    raise
                result = self._world.evaluate().to_dict()
                if not isinstance(result, dict):
                    raise TypeError("AppWorld evaluation must be a dictionary")
                # Detach the host result from AppWorld objects and require JSON output.
                self._result = json.loads(json.dumps(result))
                return dict(self._result)
            finally:
                try:
                    self._world.close()
                finally:
                    _ACTIVE_WORLD.release()

    def close(self) -> dict[str, Any]:
        """Finalize and release the world, including a final official evaluation."""
        return self.finalize()

    def __enter__(self) -> AppWorldEpisode:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        try:
            self.finalize()
        except BaseException as finalization_error:
            if exc is None:
                raise
            exc.add_note(f"AppWorld finalization also failed: {finalization_error!r}")


__all__ = ("AppWorldAgentView", "AppWorldConfig", "AppWorldEpisode")
