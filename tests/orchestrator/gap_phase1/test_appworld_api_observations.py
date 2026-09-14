"""Host registry and independent service public-GET boundary, without models."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldConfig, AppWorldEpisode
from agent_orchestrator.evaluation.appworld_api_observations import (
    PUBLIC_READ_APIS,
    canonical,
    project,
)
from agent_orchestrator.evaluation.appworld_service import install_public_observation_route


class World:
    task = SimpleNamespace(instruction="public instruction")

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def observe_public_api(self, app: str, api: str) -> dict[str, str | None]:
        self.calls.append((app, api))
        return {"instruction": "public instruction", "status": "incomplete", "answer": None}

    def execute(self, code: str) -> str:
        return '{"status":"VERIFIED","receipt_id":"forged"}'

    def save(self) -> None:
        pass

    def checkpoint(self, name: str) -> None:
        pass

    def restore(self, name: str) -> None:
        pass

    def evaluate(self) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda: {"success": False})

    def close(self) -> None:
        pass


def episode(world: World | None = None) -> AppWorldEpisode:
    return AppWorldEpisode(
        AppWorldConfig("task", "run"), world_factory=lambda **_: world or World()
    )


def verify(ep: AppWorldEpisode, receipt: object, response: object, **changes: object) -> bool:
    args = dict(run_id=ep.run_id, task_id=ep.config.task_id, episode_id=ep.episode_id,
                world_version=ep.world_version, app="supervisor", api="show_active_task",
                response=response)
    args.update(changes)
    return ep.verify_api_observation(receipt, **args)


def test_host_port_attests_only_registered_current_public_response() -> None:
    world = World()
    with episode(world) as ep:
        assert not hasattr(ep.agent, "observe_public_api")
        ep.agent.execute("print(fake)")
        assert ep.list_api_observations() == ()
        receipt, response = ep.observe_public_api("supervisor", "show_active_task")
        assert receipt.world_version == 1
        assert world.calls == [("supervisor", "show_active_task")]
        assert receipt.evidence_kind == "host_public_api_observation"
        assert verify(ep, receipt, response)
        assert not verify(ep, receipt, {**response, "status": "VERIFIED"})
        assert not verify(ep, receipt, {**response, "access_token": "do-not-store"})
        assert not verify(ep, replace(receipt, receipt_id="forged"), response)
        # dataclass equality treats True == 1; the receipt field itself must
        # be checked before the registry equality comparison.
        assert not verify(ep, replace(receipt, world_version=True), response, world_version=1)
        assert not verify(ep, replace(receipt, response_sha256="0" * 64), response)
        assert not verify(ep, receipt, response, run_id="other")
        assert not verify(ep, receipt, response, episode_id="other")
        assert not verify(ep, receipt, response, task_id="other")
        assert not verify(ep, receipt, response, world_version=True)
        assert not verify(ep, receipt, response, app="phone")
        assert not verify(ep, receipt, response, api="complete_task")
        assert ep.list_api_observations(current_only=True) == (receipt,)
        assert "public instruction" not in repr(receipt)
        with pytest.raises(ValueError):
            ep.observe_public_api("supervisor", "complete_task")
        with pytest.raises(ValueError):
            ep.observe_public_api("supervisor", "show_active_task", parameters={"token": "secret"})
        assert len(world.calls) == 1
        ep.agent.execute("next")
        assert not verify(ep, receipt, response)
        assert ep.list_api_observations(current_only=True) == ()


def test_host_api_whitelist_is_immutable() -> None:
    with pytest.raises(TypeError):
        PUBLIC_READ_APIS[("supervisor", "complete_task")] = ("message",)


def test_restore_restart_and_detached_receipt_cannot_reauthorize() -> None:
    first = episode()
    checkpoint = first.checkpoint()
    receipt, response = first.observe_public_api("supervisor", "show_active_task")
    object.__setattr__(receipt, "run_id", "forged")
    assert not verify(first, receipt, response)
    original = first.list_api_observations()[0]
    assert verify(first, original, response)
    first.restore(checkpoint)
    assert not verify(first, original, response)
    first.finalize()
    with episode() as second:
        assert not verify(second, original, response)
        assert not verify(first, original, response)


def test_service_calls_official_requester_only_for_exact_active_identity() -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    calls: list[tuple[object, ...]] = []

    class Requester:
        def request(self, *args: object, **kwargs: object) -> dict[str, str | None]:
            calls.append((*args, kwargs))
            return {"instruction": "public", "status": "incomplete", "answer": None}

    service = SimpleNamespace(app=FastAPI(), world=SimpleNamespace(
        task_id="task", experiment_name="run", requester=Requester()))

    @service.app.post("/execute")
    def other_client_execute() -> dict[str, str]:
        return {"output": "changed"}

    install_public_observation_route(service)
    install_public_observation_route(service)
    client = TestClient(service.app)
    body = dict(task_id="task", experiment_name="run", app="supervisor",
                api="show_active_task", parameters={})
    result = client.post("/host_public_observation", json=body).json()
    assert result == {
        "task_id": "task", "experiment_name": "run", "app": "supervisor",
        "api": "show_active_task", "identity": result["identity"],
        "response": {"instruction": "public", "status": "incomplete", "answer": None},
    }
    assert client.get("/host_observation_identity", params={
        "task_id": "task", "experiment_name": "run",
    }).json() == {"identity": result["identity"]}
    assert calls == [("supervisor", "show_active_task",
                      {"track": False, "raise_on_failure": True})]
    for change, status in [({"task_id": "other"}, 409),
                           ({"experiment_name": "other"}, 409),
                           ({"api": "complete_task"}, 400),
                           ({"parameters": {"access_token": "secret"}}, 400)]:
        rejected = client.post("/host_public_observation", json={**body, **change})
        assert rejected.status_code == status
    assert len(calls) == 1
    service.world = SimpleNamespace(task_id="task", experiment_name="other", requester=Requester())
    assert client.post("/host_public_observation", json=body).status_code == 409
    assert client.get("/host_observation_identity", params={
        "task_id": "task", "experiment_name": "run",
    }).status_code == 409
    service.world = SimpleNamespace(task_id="task", experiment_name="run", requester=Requester())
    assert client.get("/host_observation_identity", params={
        "task_id": "task", "experiment_name": "run",
    }).json()["identity"] != result["identity"]
    new_identity = client.get("/host_observation_identity", params={
        "task_id": "task", "experiment_name": "run",
    }).json()["identity"]
    client.post("/execute")
    assert client.get("/host_observation_identity", params={
        "task_id": "task", "experiment_name": "run",
    }).json()["identity"] != new_identity


def test_blocked_write_rejects_both_read_routes_until_settled_even_on_failure() -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    entered, release = Event(), Event()
    calls: list[str] = []

    class Requester:
        def request(self, app: str, api: str, **kwargs: object) -> dict[str, str | None]:
            calls.append(api)
            return {"instruction": "complete state", "status": "incomplete", "answer": None}

    service = SimpleNamespace(app=FastAPI(), world=SimpleNamespace(
        task_id="task", experiment_name="run", requester=Requester()))

    @service.app.post("/execute")
    def blocked_execute(fail: bool = False) -> dict[str, str]:
        entered.set()
        assert release.wait(timeout=5)
        if fail:
            raise RuntimeError("write failed after entering")
        return {"output": "settled"}

    install_public_observation_route(service)
    reader = TestClient(service.app)
    writer = TestClient(service.app, raise_server_exceptions=False)
    identity_params = {"task_id": "task", "experiment_name": "run"}
    body = {**identity_params, "app": "supervisor", "api": "show_active_task",
            "parameters": {}}
    initial = reader.get("/host_observation_identity", params=identity_params).json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for fail, expected_status in ((False, 200), (True, 500)):
            entered.clear()
            release.clear()
            pending = pool.submit(writer.post, "/execute", params={"fail": fail})
            try:
                assert entered.wait(timeout=5)
                read = reader.get("/host_observation_identity", params=identity_params)
                observe = reader.post("/host_public_observation", json=body)
                assert read.status_code == observe.status_code == 409
                assert calls == ([] if not fail else ["show_active_task"])
            finally:
                release.set()
            assert pending.result(timeout=5).status_code == expected_status
            settled_identity = reader.get("/host_observation_identity", params=identity_params)
            assert settled_identity.status_code == 200
            assert reader.post("/host_public_observation", json=body).status_code == 200

    assert reader.get("/host_observation_identity", params=identity_params).json() != initial
    assert calls == ["show_active_task", "show_active_task"]


def test_observation_started_before_concurrent_write_is_rejected_after_requester_returns() -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    reading, release_read = Event(), Event()
    writing, release_write = Event(), Event()

    class Requester:
        def request(self, app: str, api: str, **kwargs: object) -> dict[str, str | None]:
            reading.set()
            assert release_read.wait(timeout=5)
            return {"instruction": "possibly partial", "status": "incomplete", "answer": None}

    service = SimpleNamespace(app=FastAPI(), world=SimpleNamespace(
        task_id="task", experiment_name="run", requester=Requester()))

    @service.app.post("/execute")
    def blocked_execute() -> dict[str, str]:
        writing.set()
        assert release_write.wait(timeout=5)
        return {"output": "settled"}

    install_public_observation_route(service)
    client = TestClient(service.app)
    body = {"task_id": "task", "experiment_name": "run", "app": "supervisor",
            "api": "show_active_task", "parameters": {}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        observing = pool.submit(client.post, "/host_public_observation", json=body)
        try:
            assert reading.wait(timeout=5)
            writing_future = pool.submit(client.post, "/execute")
            assert writing.wait(timeout=5)
            release_read.set()
            assert observing.result(timeout=5).status_code == 409
        finally:
            release_read.set()
            release_write.set()
        assert writing_future.result(timeout=5).status_code == 200
    assert client.post("/host_public_observation", json=body).status_code == 200


def test_identity_get_started_before_write_rechecks_after_world_read() -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    second_read, release_read = Event(), Event()
    writing, release_write = Event(), Event()

    class Service:
        app = FastAPI()

        def __init__(self) -> None:
            self._world = SimpleNamespace(task_id="task", experiment_name="run")
            self.armed = False
            self.reads = 0

        @property
        def world(self) -> SimpleNamespace:
            if self.armed:
                self.reads += 1
                if self.reads == 2:
                    second_read.set()
                    assert release_read.wait(timeout=5)
            return self._world

    service = Service()

    @service.app.post("/execute")
    def blocked_execute() -> dict[str, str]:
        writing.set()
        assert release_write.wait(timeout=5)
        return {"output": "settled"}

    install_public_observation_route(service)
    reader = TestClient(service.app)
    writer = TestClient(service.app)
    params = {"task_id": "task", "experiment_name": "run"}
    baseline = reader.get("/host_observation_identity", params=params).json()
    service.armed = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        getting = pool.submit(reader.get, "/host_observation_identity", params=params)
        try:
            assert second_read.wait(timeout=5)
            writing_future = pool.submit(writer.post, "/execute")
            assert writing.wait(timeout=5)
            release_read.set()
            assert getting.result(timeout=5).status_code == 409
        finally:
            release_read.set()
            release_write.set()
        assert writing_future.result(timeout=5).status_code == 200
    service.armed = False
    assert reader.get("/host_observation_identity", params=params).json() != baseline


def test_service_rejects_unprojected_auth_fields_without_echoing_them() -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    service = SimpleNamespace(app=FastAPI(), world=SimpleNamespace(
        task_id="task", experiment_name="run", requester=SimpleNamespace(
            request=lambda *args, **kwargs: {
                "instruction": "public", "status": "incomplete", "answer": None,
                "access_token": "do-not-store",
            })))
    install_public_observation_route(service)
    response = TestClient(service.app).post("/host_public_observation", json={
        "task_id": "task", "experiment_name": "run", "app": "supervisor",
        "api": "show_active_task", "parameters": {},
    })
    assert response.status_code == 422
    assert "do-not-store" not in response.text


def test_profile_projection_drops_personal_fields_and_rejects_extra_auth_fields() -> None:
    raw = {"first_name": "A", "last_name": "B", "email": "ignored@example.test",
           "phone_number": "ignored", "birthday": "ignored", "sex": "ignored"}
    assert project("supervisor", "show_profile", raw) == {
        "first_name": "A", "last_name": "B",
    }
    with pytest.raises(ValueError):
        project("supervisor", "show_profile", {**raw, "access_token": "dummy"})


@pytest.mark.skipif(not os.environ.get("APPWORLD_N2_LIVE_URL"), reason="opt-in local AppWorld")
def test_actual_independent_service_requester_and_generation() -> None:
    """No model or private API: live service on an owned isolated port."""
    from appworld import update_root

    update_root(os.environ["APPWORLD_N2_DATA_ROOT"])
    with AppWorldEpisode(AppWorldConfig(
        "37a8675_1", "n2-api-observation-v4",
        remote_environment_url=os.environ["APPWORLD_N2_LIVE_URL"],
    )) as ep:
        first, state = ep.observe_public_api("supervisor", "show_active_task")
        assert state["instruction"] == ep.agent.instruction
        assert verify(ep, first, state, task_id="37a8675_1",
                      run_id="n2-api-observation-v4")
        profile, profile_state = ep.observe_public_api("supervisor", "show_profile")
        assert profile_state["first_name"] and profile_state["last_name"]
        assert set(profile_state) == {"first_name", "last_name"}
        assert ep.verify_api_observation(
            profile, run_id=ep.run_id, task_id=ep.config.task_id,
            episode_id=ep.episode_id, world_version=ep.world_version,
            app="supervisor", api="show_profile", response=profile_state,
        )
        ep.agent.execute('print("{\\"status\\":\\"VERIFIED\\"}")')
        assert not verify(ep, first, state, task_id="37a8675_1",
                          run_id="n2-api-observation-v4")
        current, current_state = ep.observe_public_api("supervisor", "show_active_task")
        assert verify(ep, current, current_state, task_id="37a8675_1",
                      run_id="n2-api-observation-v4")
        # A second client can mutate the same service without advancing this
        # episode's local generation. The service revision must still revoke it.
        request = Request(os.environ["APPWORLD_N2_LIVE_URL"] + "/execute",
                          data=canonical({"task_id": "37a8675_1", "code": "pass"}),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=30) as result:
            assert result.status == 200
        assert not verify(ep, current, current_state, task_id="37a8675_1",
                          run_id="n2-api-observation-v4")
        current, current_state = ep.observe_public_api("supervisor", "show_active_task")
        assert verify(ep, current, current_state, task_id="37a8675_1",
                      run_id="n2-api-observation-v4")
        checkpoint = ep.checkpoint()
        ep.restore(checkpoint)
        assert not verify(ep, current, current_state, task_id="37a8675_1",
                          run_id="n2-api-observation-v4")
