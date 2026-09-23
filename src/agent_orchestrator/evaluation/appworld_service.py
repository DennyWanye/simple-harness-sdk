"""Optional AppWorld 0.1.3.post1 service with restored-checkpoint clock repair.

The published load_state closes all clocks but omits restoring the task clock;
subsequent close then stops an already-stopped freezer. Keep this compatibility
change explicit, version-bound and outside the agent process and scoring process.
"""

from __future__ import annotations

from importlib import import_module
from threading import Lock
from typing import Any
from uuid import uuid4

from .appworld_api_observations import PUBLIC_READ_APIS, project
from .appworld_api_observations import digest
from .appworld_state_observations import AppWorldStatePolicy, load_state_policies


def install_public_observation_route(environment: Any, *,
                                     state_policies: dict[str, AppWorldStatePolicy] | None = None) -> None:
    """Install once on the independent environment service, never upstream."""
    fastapi: Any = import_module("fastapi")
    Body, HTTPException = fastapi.Body, fastapi.HTTPException

    if any(route.path == "/host_public_observation" for route in environment.app.routes):
        return

    service_id = uuid4().hex
    last_world: list[Any] = [None]
    world_id: list[str] = [""]
    revision: list[int] = [0]
    inflight_mutations: list[int] = [0]
    state_lock = Lock()
    registered_states = dict(state_policies or {})
    if any(key != policy.content_hash for key, policy in registered_states.items()):
        raise ValueError("state policy registration hash differs")

    @environment.app.get("/host_state_policy_registry")
    def host_state_policy_registry() -> dict[str, Any]:
        hashes = sorted(registered_states)
        return {"policy_hashes": hashes, "registry_hash": digest(hashes)}

    @environment.app.middleware("http")
    async def track_external_world_mutations(request: Any, call_next: Any) -> Any:
        # Includes other SDK clients talking to this service. Advance before
        # even a failed write: partial effects must invalidate prior evidence.
        mutates_world = request.method == "POST" and request.url.path in {
            "/initialize", "/execute", "/load_state", "/close", "/close_all",
        }
        if mutates_world:
            with state_lock:
                revision[0] += 1
                inflight_mutations[0] += 1
        try:
            return await call_next(request)
        finally:
            if mutates_world:
                with state_lock:
                    inflight_mutations[0] -= 1

    def identity(active: Any) -> str:
        with state_lock:
            if inflight_mutations[0]:
                raise HTTPException(status_code=409, detail="world mutation in progress")
            if active is not last_world[0]:
                last_world[0] = active
                world_id[0] = uuid4().hex
            return service_id + ":" + world_id[0] + ":" + str(revision[0])

    @environment.app.get("/host_observation_identity")
    def host_observation_identity(task_id: str, experiment_name: str) -> dict[str, str]:
        active = environment.world
        if (active is None or active.task_id != task_id
                or active.experiment_name != experiment_name):
            raise HTTPException(status_code=409, detail="active world identity differs")
        observed_identity = identity(active)
        if environment.world is not active or identity(active) != observed_identity:
            raise HTTPException(status_code=409, detail="active world changed")
        return {"identity": observed_identity}

    @environment.app.get("/host_dataset_identity")
    def host_dataset_identity(task_id: str, experiment_name: str) -> dict[str, str]:
        from pathlib import Path
        from .appworld_state_observations import task_dataset_hash
        active = environment.world
        if (active is None or active.task_id != task_id or active.experiment_name != experiment_name):
            raise HTTPException(status_code=409, detail="active world identity differs")
        observed_identity = identity(active)
        try:
            data_root = Path(active.task.models_from_db_home_path).resolve().parents[2]
            dataset_hash = task_dataset_hash(data_root, task_id)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="actual task inputs are unavailable") from exc
        if environment.world is not active or identity(active) != observed_identity:
            raise HTTPException(status_code=409, detail="active world changed")
        return {"identity": observed_identity, "dataset_hash": dataset_hash}

    @environment.app.post("/host_public_observation")
    def host_public_observation(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        # Fail closed before Requester sees an app, API or any parameters.
        if set(body) != {"task_id", "experiment_name", "app", "api", "parameters"}:
            raise HTTPException(status_code=400, detail="invalid observation request")
        task_id, experiment_name = body["task_id"], body["experiment_name"]
        app, api = body["app"], body["api"]
        if (not isinstance(task_id, str) or not isinstance(experiment_name, str)
                or not isinstance(app, str) or not isinstance(api, str)
                or (app, api) not in PUBLIC_READ_APIS or body["parameters"] != {}):
            raise HTTPException(status_code=400, detail="API is not authorized for observation")
        active = environment.world
        if (active is None or active.task_id != task_id
                or active.experiment_name != experiment_name):
            raise HTTPException(status_code=409, detail="active world identity differs")
        observed_identity = identity(active)
        try:
            response = active.requester.request(app, api, track=False, raise_on_failure=True)
            selected = project(app, api, response)
        except Exception as exc:
            # Never include raw upstream response/credentials in HTTP errors.
            raise HTTPException(status_code=422, detail="public observation failed") from exc
        if environment.world is not active or identity(active) != observed_identity:
            raise HTTPException(status_code=409, detail="active world changed")
        return {"task_id": task_id, "experiment_name": experiment_name,
                "app": app, "api": api, "response": selected,
                "identity": observed_identity}

    @environment.app.post("/host_state_observation")
    def host_state_observation(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if (set(body) != {"task_id", "experiment_name", "policy_hash"}
                or any(not isinstance(value, str) for value in body.values())):
            raise HTTPException(status_code=400, detail="invalid state observation request")
        policy = registered_states.get(body["policy_hash"])
        if policy is None:
            raise HTTPException(status_code=403, detail="state policy is not registered")
        active = environment.world
        if (active is None or active.task_id != body["task_id"]
                or active.experiment_name != body["experiment_name"]):
            raise HTTPException(status_code=409, detail="active world identity differs")
        observed_identity = identity(active)
        try:
            result = policy.observe(active)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="registered state observation failed") from exc
        if environment.world is not active or identity(active) != observed_identity:
            raise HTTPException(status_code=409, detail="active world changed")
        return {**body, "identity": observed_identity, "result": result}


def serve(root: str, *, port: int = 18244, state_policy_file: str | None = None) -> None:
    from importlib import import_module
    from importlib.metadata import version

    uvicorn = import_module("uvicorn")
    appworld: Any = import_module("appworld")
    environment: Any = import_module("appworld.serve.environment")

    if version("appworld") != "0.1.3.post1":
        raise RuntimeError("Revalidate AppWorld checkpoint clock compatibility for this version")

    class CheckpointClockWorld(appworld.AppWorld):
        def load_state(self, state_id: str) -> None:
            super().load_state(state_id)
            if not self.remote_environment_url:
                self._set_datetime()

    appworld.update_root(root)
    environment.AppWorld = CheckpointClockWorld
    install_public_observation_route(environment, state_policies=dict(load_state_policies(state_policy_file)))
    uvicorn.run(environment.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=18244)
    parser.add_argument("--state-policy-file")
    args = parser.parse_args()
    serve(args.root, port=args.port, state_policy_file=args.state_policy_file)
