# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import pytest

from simple_harness.testing import (
    PROTOCOL_VERSION,
    CaseObservation,
    CaseStatus,
    ConformanceHostMetadata,
    run_conformance,
)
from simple_harness.testing.suites import CASES_BY_SUITE
from simple_harness import __version__


ARTIFACT_SHA = "a" * 64
SECRET_CANARY = "sk-conformance-secret-DO-NOT-LEAK"


GOOD_VALUES = {
    "provider.physical_request": {"physical_calls": 1, "request_id": "req-1", "response_request_id": "req-1"},
    "provider.typed_error": {"physical_calls": 1, "error_code": "provider_transport_error", "raw_body_exposed": False},
    "provider.usage": {"trusted_total_tokens": 3, "unknown_usage": None},
    "provider.redaction": {"secret": SECRET_CANARY, "public_text": "[REDACTED]", "raw_body_exposed": False},
    "tool.schema": {"closed": True, "bounded": True, "reserved_fields_rejected": True},
    "tool.five_state": {"states": ["succeeded", "partial", "rejected", "failed", "unknown"]},
    "tool.reconcile": {"initial_state": "unknown", "final_state": "succeeded", "physical_calls_before": 1, "physical_calls_after": 1},
    "tool.malformed_duplicate_late": {"accepted_results": 1, "rejected_results": 3, "physical_calls": 1},
    "runtime.no_tool": {"terminal_state": "completed", "provider_calls": 1, "tool_calls": 0},
    "runtime.one_tool": {"terminal_state": "completed", "provider_calls": 2, "tool_calls": 1, "correlation_match": True},
    "runtime.multi_turn_tool": {"terminal_state": "completed", "provider_calls": 3, "tool_calls": 2, "unique_call_ids": True},
    "runtime.session_persistence": {"reopened": True, "session_before": "session-1", "session_after": "session-1"},
    "runtime.hitl": {"physical_calls_before": 0, "physical_calls_after": 1, "decision": "approved", "durable": True},
    "runtime.delivery": {"attempts": 2, "deliveries": 1, "settled": True},
    "runtime.budget": {"terminations": ["max_turns", "max_tool_calls", "wall_clock", "cost", "repeated_tool"]},
    "runtime.restart_without_replay": {"reopened": True, "physical_calls_before": 1, "physical_calls_after": 1, "reconciled": True},
    "workflow.host_owned": {"registered": True, "completed": True, "definition_id": "host.workflow"},
    "workflow.official_durable_task": {"profile_key": "workflow.durable_task", "completed": True},
    "workflow.official_personal_v1": {"profile_key": "workflow.personal_v1", "completed": True},
    "workflow.official_capability_build": {"profile_key": "workflow.capability_build", "completed": True},
    "workflow.ticket_fingerprint": {"forged_ticket_rejected": True, "fingerprint_rejected": True, "child_runs": 0},
    "workflow.reopen": {"reopened": True, "run_before": "run-1", "run_after": "run-1", "physical_calls_before": 1, "physical_calls_after": 1, "completed": True},
}


class _Suite:
    def __init__(
        self,
        host: "FakeHost",
        name: str,
        *,
        overrides: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.host = host
        self.name = name
        self.overrides = dict(overrides or {})
        self.closed = False

    async def _case(self, case_id: str) -> CaseObservation:
        self.host.case_calls.append(case_id)
        values = dict(GOOD_VALUES[case_id])
        values.update(self.overrides.get(case_id, {}))
        return CaseObservation(
            case_id=case_id,
            values=values,
            evidence={
                "authorization": f"Bearer {SECRET_CANARY}",
                "raw_provider_body": f"raw {SECRET_CANARY}",
                "safe": "receipt-123",
            },
        )

    async def physical_request(self): return await self._case("provider.physical_request")
    async def typed_error(self): return await self._case("provider.typed_error")
    async def usage(self): return await self._case("provider.usage")
    async def redaction(self): return await self._case("provider.redaction")
    async def schema(self): return await self._case("tool.schema")
    async def five_state(self): return await self._case("tool.five_state")
    async def reconcile(self): return await self._case("tool.reconcile")
    async def malformed_duplicate_late(self): return await self._case("tool.malformed_duplicate_late")
    async def no_tool(self): return await self._case("runtime.no_tool")
    async def one_tool(self): return await self._case("runtime.one_tool")
    async def multi_turn_tool(self): return await self._case("runtime.multi_turn_tool")
    async def session_persistence(self): return await self._case("runtime.session_persistence")
    async def hitl(self): return await self._case("runtime.hitl")
    async def delivery(self): return await self._case("runtime.delivery")
    async def budget(self): return await self._case("runtime.budget")
    async def restart_without_replay(self): return await self._case("runtime.restart_without_replay")
    async def host_owned(self): return await self._case("workflow.host_owned")
    async def official_durable_task(self): return await self._case("workflow.official_durable_task")
    async def official_personal_v1(self): return await self._case("workflow.official_personal_v1")
    async def official_capability_build(self): return await self._case("workflow.official_capability_build")
    async def ticket_fingerprint(self): return await self._case("workflow.ticket_fingerprint")
    async def reopen(self): return await self._case("workflow.reopen")

    async def aclose(self) -> None:
        assert not self.closed
        self.closed = True
        self.host.close_calls.append(self.name)


class _SuiteContext:
    def __init__(self, suite: _Suite) -> None:
        self.suite = suite

    async def __aenter__(self) -> _Suite:
        return self.suite

    async def __aexit__(self, *_: object) -> None:
        await self.suite.aclose()


class FakeHost:
    def __init__(
        self,
        *,
        protocol_version: str = PROTOCOL_VERSION,
        capabilities: frozenset[str] = frozenset(CASES_BY_SUITE),
        overrides: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.metadata = ConformanceHostMetadata(
            protocol_version=protocol_version,
            host_name="fixture-host",
            host_version="7.4.0",
            capabilities=capabilities,
        )
        self.overrides = dict(overrides or {})
        self.open_calls: list[str] = []
        self.close_calls: list[str] = []
        self.case_calls: list[str] = []
        self.contexts: list[_SuiteContext] = []

    def open_suite(self, name: str) -> _SuiteContext:
        self.open_calls.append(name)
        context = _SuiteContext(_Suite(self, name, overrides=self.overrides))
        self.contexts.append(context)
        return context


def _run(host: FakeHost, suites: tuple[str, ...] = tuple(CASES_BY_SUITE)):
    return asyncio.run(run_conformance(lambda: host, suites, artifact_sha256=ARTIFACT_SHA))


def test_all_suites_use_fresh_async_context_and_close() -> None:
    host = FakeHost()
    report = _run(host)

    assert report.passed
    assert host.open_calls == list(CASES_BY_SUITE)
    assert host.close_calls == list(CASES_BY_SUITE)
    assert len({id(context) for context in host.contexts}) == len(CASES_BY_SUITE)
    assert host.case_calls == [
        case.case_id
        for suite in CASES_BY_SUITE.values()
        for case in suite
    ]
    assert len(report.cases) == len(host.case_calls)


def test_protocol_major_mismatch_fails_before_opening_suite() -> None:
    host = FakeHost(protocol_version="2.0.0")
    report = _run(host, ("provider",))

    assert not report.passed
    assert report.errors[0].code == "protocol_major_mismatch"
    assert host.open_calls == []


def test_missing_required_capability_fails_before_opening_suite() -> None:
    host = FakeHost(capabilities=frozenset({"tool"}))
    report = _run(host, ("provider",))

    assert not report.passed
    assert report.errors[0].code == "missing_capability"
    assert host.open_calls == []


def test_sdk_verifier_rejects_host_observation_that_violates_case_contract() -> None:
    host = FakeHost(overrides={"provider.physical_request": {"physical_calls": 0}})

    report = _run(host, ("provider",))

    assert not report.passed
    assert report.cases[0].status is CaseStatus.FAIL
    assert report.cases[0].message == "SDK verifier rejected the Host observation."


def test_arbitrary_host_exception_text_never_enters_report() -> None:
    host = FakeHost()

    def open_suite(name: str) -> _SuiteContext:
        suite = _Suite(host, name)

        async def explode() -> CaseObservation:
            raise RuntimeError(
                "raw provider body PASSWORD=hunter2 cookie=session-secret arbitrary-canary"
            )

        suite.physical_request = explode  # type: ignore[method-assign]
        return _SuiteContext(suite)

    host.open_suite = open_suite  # type: ignore[method-assign]
    report = _run(host, ("provider",))
    rendered = json.dumps(report.to_json(), sort_keys=True)

    assert not report.passed
    assert report.cases[0].status is CaseStatus.ERROR
    for canary in ("hunter2", "session-secret", "arbitrary-canary", "raw provider"):
        assert canary not in rendered
    assert report.cases[0].message == "Host operation failed."


def test_report_json_is_fixed_metadata_and_redacted() -> None:
    report = _run(FakeHost(), ("provider",))
    payload = report.to_json()
    rendered = json.dumps(payload, sort_keys=True)

    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["sdk_version"] == __version__
    assert payload["host"] == {"name": "fixture-host", "version": "7.4.0"}
    assert payload["artifact_sha256"] == ARTIFACT_SHA
    assert payload["platform"]
    assert payload["python_version"].startswith("3.")
    assert SECRET_CANARY not in rendered
    assert "raw " not in rendered
    assert "receipt-123" in rendered
    assert "[REDACTED]" in rendered
