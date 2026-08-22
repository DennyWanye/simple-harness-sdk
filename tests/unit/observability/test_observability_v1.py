# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from simple_harness.observability import (
    CompositeSink,
    CorrelationContext,
    JsonlSink,
    LoggingSink,
    NoopSink,
    ObservabilityEventV1,
    ObservabilityRuntime,
    Outcome,
    RecordingSink,
    RingBufferSink,
    SafeEmitter,
    Severity,
    UnsafeAttributeError,
    validate_event_dict,
)


def event(sequence: int = 1, attributes: dict[str, object] | None = None) -> ObservabilityEventV1:
    return ObservabilityEventV1(
        event_name="harness.run.started",
        occurred_at=1_700_000_000.25,
        sequence=sequence,
        severity=Severity.INFO,
        component="harness",
        operation="run.start",
        outcome=Outcome.STARTED,
        correlation=CorrelationContext(
            trace_id="trace-1", root_id="root-1", operation_id="operation-1"
        ),
        attributes=attributes or {"attempt": 1, "stage": "accepted"},
    )


def test_schema_is_immutable_canonical_and_forward_compatible() -> None:
    value = event()
    with pytest.raises(FrozenInstanceError):
        value.sequence = 9  # type: ignore[misc]
    with pytest.raises(TypeError):
        value.attributes["attempt"] = 2  # type: ignore[index]
    encoded = value.to_dict()
    assert list(encoded) == [
        "schema_version",
        "event_name",
        "occurred_at",
        "sequence",
        "severity",
        "component",
        "operation",
        "outcome",
        "correlation",
        "attributes",
    ]
    validate_event_dict({**encoded, "future_extension": {"ignored": True}})


@pytest.mark.parametrize(
    "attributes",
    [
        {"content": "MEMORY正文-CANARY"},
        {"error_code": "MEMORY正文-CANARY"},
        {"error_code": "sk-live-API-KEY-CANARY"},
        {"exception": RuntimeError("MEMORY正文-CANARY")},
        {"drop_reason": {"unknown": "MEMORY正文-CANARY"}},
        {"unknown": {"nested": {"authorization": "Bearer API-KEY-CANARY"}}},
    ],
)
def test_default_deny_attributes_reject_privacy_canaries(attributes: dict[str, object]) -> None:
    with pytest.raises(UnsafeAttributeError):
        event(attributes=attributes)


def test_failing_sink_isolated_and_counted() -> None:
    class Failing:
        def emit(self, value: ObservabilityEventV1) -> None:
            raise RuntimeError(value.event_name)

    emitter = SafeEmitter(Failing())
    assert emitter.emit(event())
    assert emitter.flush(1)
    assert emitter.counters.sink_errors == 1
    assert emitter.counters.dropped == 1
    assert emitter.close()


def test_reentrant_sink_is_dropped_without_recursion() -> None:
    class Reentrant:
        emitter: SafeEmitter

        def emit(self, value: ObservabilityEventV1) -> None:
            self.emitter.emit(value)

    sink = Reentrant()
    emitter = SafeEmitter(sink)
    sink.emitter = emitter
    assert emitter.emit(event())
    assert emitter.flush(1)
    assert emitter.counters.emitted == 1
    assert emitter.counters.reentrant_dropped == 1
    emitter.close()


def test_blocking_sink_never_blocks_emit_or_bounded_close() -> None:
    entered = threading.Event()
    release = threading.Event()

    class Blocking:
        def emit(self, value: ObservabilityEventV1) -> None:
            entered.set()
            release.wait(5)

    emitter = SafeEmitter(Blocking(), capacity=1, close_timeout=0.02)
    started = time.monotonic()
    assert emitter.emit(event())
    assert time.monotonic() - started < 0.05
    assert entered.wait(1)
    started = time.monotonic()
    assert not emitter.close()
    assert time.monotonic() - started < 0.1
    assert emitter.counters.close_timeouts == 1
    assert not emitter.emit(event())
    release.set()


def test_ring_overflow_is_drop_oldest_and_deterministic() -> None:
    ring = RingBufferSink(2)
    ring.emit(event(1))
    ring.emit(event(2))
    ring.emit(event(3))
    assert [item.sequence for item in ring.events()] == [2, 3]
    assert ring.overflow_count == 1


def test_composite_continues_after_child_failure() -> None:
    recorded = RecordingSink()

    class Failing:
        def emit(self, value: ObservabilityEventV1) -> None:
            raise RuntimeError

    composite = CompositeSink((Failing(), recorded))
    composite.emit(event())
    assert composite.flush(1)
    assert len(recorded.events()) == 1
    assert composite.diagnostics() == {"accepted_children": 2, "failed_children": 1}
    composite.close()


def test_composite_blocking_child_does_not_delay_healthy_child() -> None:
    entered = threading.Event()
    release = threading.Event()
    recorded = RecordingSink()

    class Blocking:
        def emit(self, value: ObservabilityEventV1) -> None:
            entered.set()
            release.wait(5)

    composite = CompositeSink((Blocking(), recorded), close_timeout=0.02)
    composite.emit(event())
    assert entered.wait(1)
    deadline = time.monotonic() + 1
    while not recorded.events() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert len(recorded.events()) == 1
    assert not composite.close(0.02)
    release.set()


def test_jsonl_rotation_permissions_and_no_symlink(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path, max_bytes=700, max_files=2)
    for sequence in range(1, 8):
        sink.emit(event(sequence))
    files = sorted(tmp_path.glob("events.jsonl*"))
    assert [item.name for item in files] == ["events.jsonl", "events.jsonl.1"]
    assert all(os.stat(item).st_mode & 0o777 == 0o600 for item in files)
    for item in files:
        for line in item.read_text(encoding="utf-8").splitlines():
            validate_event_dict(json.loads(line))

    target = tmp_path / "target"
    target.write_text("do-not-touch", encoding="utf-8")
    link = tmp_path / "linked.jsonl"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        JsonlSink(link)
    assert target.read_text(encoding="utf-8") == "do-not-touch"


def test_logging_sink_contains_only_structured_safe_event(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("observability-contract-test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        LoggingSink(logger).emit(event())
    record = caplog.records[-1]
    assert record.message == "harness.run.started"
    serialized = json.dumps(record.simple_harness_observability)  # type: ignore[attr-defined]
    assert "MEMORY正文-CANARY" not in serialized


def test_concurrent_emit_snapshot_and_close_are_safe() -> None:
    recorded = RecordingSink(capacity=5000)
    emitter = SafeEmitter(recorded, capacity=4096)
    failures: list[BaseException] = []

    def produce() -> None:
        try:
            for _ in range(200):
                emitter.emit(event())
                emitter.diagnostics_snapshot()
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=produce) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert emitter.close(2)
    assert not failures
    counters = emitter.counters
    assert counters.accepted == counters.emitted
    assert counters.dropped == 0
    assert emitter.close()


def test_noop_and_emit_after_close_have_stable_counters() -> None:
    emitter = SafeEmitter(NoopSink())
    assert emitter.emit(event())
    assert emitter.close()
    assert not emitter.emit(event())
    assert emitter.counters.emit_after_close == 1


def test_observability_import_does_not_load_runtime_execution_or_sqlite() -> None:
    code = """
import sys
import simple_harness.observability
for prefix in ('simple_harness.runtime', 'simple_harness.execution',
               'simple_harness.providers', 'simple_harness.tools', 'sqlite3'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_authority_correlation_reconstructs_without_exposing_source_ids() -> None:
    first = CorrelationContext.from_authority_ids(
        run_id="run-private-1",
        execution_session_id="session-private-1",
        request_id="request-private-1",
        call_id="call-private-1",
        effect_id="effect-private-1",
    )
    second = CorrelationContext.from_authority_ids(
        run_id="run-private-1",
        execution_session_id="session-private-1",
        request_id="request-private-1",
        operation_id="outbox-private-1",
    )
    assert first.trace_id == second.trace_id
    assert first.root_id == second.root_id
    serialized = json.dumps([first.to_dict(), second.to_dict()])
    assert "private" not in serialized
    assert all(len(value) == 64 for value in first.to_dict().values())


@pytest.mark.parametrize(
    "attributes",
    [
        {"error_code": "exception-MEMORY正文-CANARY"},
        {"error_code": "sk-live-API-KEY-CANARY"},
        {"content": "provider-response-CANARY"},
    ],
)
def test_runtime_forbidden_content_canaries_are_isolated(
    attributes: dict[str, object],
) -> None:
    sink = RecordingSink()
    runtime = ObservabilityRuntime(sink)
    assert not runtime.emit_transition(
        "provider_attempt.failed",
        component="provider",
        operation="invoke",
        outcome=Outcome.FAILED,
        correlation=CorrelationContext.new_root(),
        attributes=attributes,
    )
    assert runtime.flush(1)
    assert not sink.events()
    assert runtime.counters.dropped == 1
    runtime.close()
