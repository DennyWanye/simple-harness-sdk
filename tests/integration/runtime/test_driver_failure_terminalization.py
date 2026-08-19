# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging

from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.runtime import (
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)


class _NoopPort:
    async def reconcile(self) -> None:
        return None


class _Catalog:
    def __init__(self, generation: int = 1) -> None:
        self.generation = generation

    def current_generation(self) -> int:
        return self.generation


class _RaisingDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def start(self, invocation, *, context, cancel):
        del invocation, context, cancel
        self.calls += 1
        raise RuntimeError("driver boom")


def _request(name: str = "one", *, generation: int = 1) -> RunStart:
    return RunStart(
        ExecutionSessionId("session-1"),
        RunId(f"run-{name}"),
        RequestId(f"request-{name}"),
        f"turn-{name}",
        {"prompt": name},
        generation,
    )


def _runtime(tmp_path, driver):
    database = Database.open(tmp_path / "runtime.db")
    uow = SqliteExecutionUnitOfWork(database)
    noop = _NoopPort()
    value = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": driver},
        RuntimePorts(
            provider=noop,
            tools=noop,
            authorization=noop,
            context=SqliteContextPort(database),
            delivery=noop,
            tool_reconciliation=noop,
            reconciliation=noop,
            provider_reconciliation=noop,
            react_checkpoint=uow,
            tool_catalog=_Catalog(),
            owner_id="owner-1",
        ),
    )
    return value, uow, database


def test_driver_exception_terminalizes_failed_without_secondary_logger_error(
    tmp_path, caplog
) -> None:
    """A raising driver must durable-terminalize to FAILED and the failure log
    must not itself raise a secondary TypeError (regression for the bare
    `run_id=` kwarg that previously broke the error path)."""
    caplog.set_level(logging.ERROR)

    async def case() -> None:
        driver = _RaisingDriver()
        value, _, database = _runtime(tmp_path, driver)
        await value.start()
        await value.client.start(_request("boom"))
        await value.wait_idle(RunId("run-boom"))
        record = value.client.query(RunId("run-boom"))
        assert record is not None
        assert record.state is RunState.FAILED
        row = database.connection.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? AND kind='run.failed'",
            ("run-boom",),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        assert payload.get("code") == "driver_failed"
        assert "private_cause" not in payload
        assert driver.calls == 1
        await value.close()
        database.close()

    asyncio.run(case())

    # The error path itself must not throw: no logging TypeError surfaced.
    assert "unexpected keyword argument" not in caplog.text
    assert "sdk_run_driver_failed" in caplog.text
