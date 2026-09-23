"""Cold attach to the same surviving AppWorld service at a quiescent checkpoint.

This restores the host observation ledgers, not the remote world. Any service/world
identity change refuses recovery before execution. No initialize/load_state is sent.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any
from urllib.request import Request, urlopen

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from .appworld import (
    _ACTIVE_WORLD,
    AppWorldAgentView,
    AppWorldConfig,
    AppWorldEpisode,
    _ExternallyScoredWorld,
)
from .appworld_api_observations import AppWorldAPILedger, AppWorldAPIReceipt
from .appworld_observations import AppWorldExecutionReceipt, AppWorldObservationLedger
from .appworld_state_observations import AppWorldStateReceipt


def durable_document(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as stream:
        stream.write(canonical_json({"state": state, "sha256": content_hash_of(state)}))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_document(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if set(data) != {"state", "sha256"} or content_hash_of(data["state"]) != data["sha256"]:
        raise ContractError("recovery checkpoint integrity differs")
    return data["state"]


def checkpoint_episode(episode: AppWorldEpisode, path: Path) -> None:
    with episode._lock:
        if episode._finalized or not isinstance(episode._world, _ExternallyScoredWorld):
            raise ContractError("only a live remote AppWorld episode can be attached")
        identity = episode._world.public_observation_identity()
        state = {
            "config": asdict(episode.config),
            "instruction": episode.agent.instruction,
            "dataset_hash": episode.dataset_identity(),
            "service_identity": identity,
            "episode_id": episode.episode_id,
            "world_version": episode.world_version,
            "save_number": episode._world._save_number,
            "checkpoints": sorted(episode._checkpoints),
            "execution_receipts": [asdict(r) for r in episode.list_execution_receipts()],
            "api_receipts": [asdict(r) for r in episode._api_observations.list_receipts()],
            "state_receipts": [asdict(r) for r in episode._state_observations.values()],
        }
        if identity != episode._world.public_observation_identity():
            raise ContractError("remote world changed during recovery checkpoint")
        durable_document(path, state)


class _AttachedRemoteTransport:
    """Published remote methods only; construction intentionally makes no requests."""

    def __init__(self, config: AppWorldConfig, instruction: str) -> None:
        self.remote_environment_url = str(config.remote_environment_url)
        self.task_id = config.task_id
        self.task = SimpleNamespace(instruction=instruction)

    def _call(self, method: str, **values: Any) -> Any:
        request = Request(
            self.remote_environment_url.rstrip("/") + "/" + method,
            data=canonical_json({"task_id": self.task_id, **values}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ContractError("AppWorld response exceeds bound")
        return json.loads(raw)["output"]

    def execute(self, code: str) -> str:
        value = self._call("execute", code=code)
        if not isinstance(value, str):
            raise ContractError("AppWorld execute response is not text")
        return value

    def save_state(self, *, state_id: str) -> Any:
        return self._call("save_state", state_id=state_id)

    def load_state(self, *, state_id: str) -> Any:
        return self._call("load_state", state_id=state_id)

    def close(self) -> None:
        self._call("close")


def attach_episode(config: AppWorldConfig, path: Path) -> AppWorldEpisode:
    state = read_document(path)
    if state["config"] != asdict(config) or not config.remote_environment_url:
        raise ContractError("AppWorld attach configuration differs")
    world = _ExternallyScoredWorld(
        _AttachedRemoteTransport(config, state["instruction"]),
        config.task_id,
        config.experiment_name,
    )
    if (
        world.public_observation_identity() != state["service_identity"]
        or world.dataset_identity() != state["dataset_hash"]
    ):
        raise ContractError("original AppWorld service/world is no longer available")
    if not _ACTIVE_WORLD.acquire(blocking=False):
        raise ContractError("another AppWorld episode owns this process")
    try:
        episode = AppWorldEpisode.__new__(AppWorldEpisode)
        episode.config, episode._world = config, world
        episode._lock, episode._finalized, episode._result = RLock(), False, None
        episode._checkpoints = set(state["checkpoints"])
        episode._world_change_listeners = []
        observations = AppWorldObservationLedger(
            run_id=config.experiment_name, task_id=config.task_id
        )
        observations.episode_id, observations.world_version = (
            state["episode_id"],
            state["world_version"],
        )
        observations._receipts = {
            r["execution_id"]: AppWorldExecutionReceipt(**r) for r in state["execution_receipts"]
        }
        api = AppWorldAPILedger(
            run_id=config.experiment_name, task_id=config.task_id, episode_id=state["episode_id"]
        )
        api._registered = {r["receipt_id"]: AppWorldAPIReceipt(**r) for r in state["api_receipts"]}
        episode._observations, episode._api_observations = observations, api
        episode._state_observations = {
            r["receipt_id"]: AppWorldStateReceipt(**r) for r in state["state_receipts"]
        }
        receipts: tuple[
            AppWorldExecutionReceipt | AppWorldAPIReceipt | AppWorldStateReceipt, ...
        ] = (
            *observations._receipts.values(),
            *api._registered.values(),
            *episode._state_observations.values(),
        )
        for receipt in receipts:
            if (receipt.run_id, receipt.episode_id, receipt.task_id) != (
                config.experiment_name,
                state["episode_id"],
                config.task_id,
            ):
                raise ContractError("restored AppWorld receipt belongs to another episode")
            if not 0 <= receipt.world_version <= observations.world_version:
                raise ContractError("restored AppWorld receipt generation differs")
        world._save_number = state["save_number"]
        episode.agent = AppWorldAgentView(state["instruction"], episode._execute)
        if world.public_observation_identity() != state["service_identity"]:
            raise ContractError("remote world changed while attaching")
        return episode
    except BaseException:
        _ACTIVE_WORLD.release()
        # Never close the remote world on failed attach; it belongs to its original host.
        raise
