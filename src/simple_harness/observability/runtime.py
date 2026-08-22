# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Non-blocking emitter and bounded worker lifecycle."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from simple_harness.version import __version__

from .contracts import ObservabilityEventV1, Outcome, Severity
from .correlation import CorrelationContext
from .redaction import SafeValue
from .sinks import CompositeSink, NoopSink, ObservabilitySink
from .snapshot import DiagnosticsSnapshotV1


@dataclass(frozen=True, slots=True)
class EmitterCounters:
    accepted: int = 0
    emitted: int = 0
    dropped: int = 0
    overflow_dropped: int = 0
    sink_errors: int = 0
    reentrant_dropped: int = 0
    emit_after_close: int = 0
    close_timeouts: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "accepted": self.accepted,
            "emitted": self.emitted,
            "dropped": self.dropped,
            "overflow_dropped": self.overflow_dropped,
            "sink_errors": self.sink_errors,
            "reentrant_dropped": self.reentrant_dropped,
            "emit_after_close": self.emit_after_close,
            "close_timeouts": self.close_timeouts,
        }


class SafeEmitter:
    """Queue events without invoking a sink on the caller's thread."""

    def __init__(
        self,
        sink: ObservabilitySink | None = None,
        *,
        capacity: int = 256,
        close_timeout: float = 1.0,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self._sink = sink or NoopSink()
        self._capacity = capacity
        self._close_timeout = float(close_timeout)
        self._queue: queue.Queue[ObservabilityEventV1] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._state = "open"
        self._pending = 0
        self._sequence = 0
        self._counters = EmitterCounters()
        self._worker = threading.Thread(
            target=self._run,
            name="simple-harness-observability",
            daemon=True,
        )
        self._worker.start()

    def _bump(self, **changes: int) -> None:
        values = self._counters.to_dict()
        for name, increment in changes.items():
            values[name] += increment
        self._counters = EmitterCounters(**values)

    def emit(self, event: ObservabilityEventV1) -> bool:
        if not isinstance(event, ObservabilityEventV1):
            with self._lock:
                self._bump(dropped=1)
            return False
        if threading.current_thread() is self._worker:
            with self._lock:
                self._bump(dropped=1, reentrant_dropped=1)
            return False
        with self._lock:
            if self._state != "open":
                self._bump(dropped=1, emit_after_close=1)
                return False
            self._sequence += 1
            queued = replace(event, sequence=self._sequence)
            try:
                self._queue.put_nowait(queued)
            except queue.Full:
                self._bump(dropped=1, overflow_dropped=1)
                return False
            self._pending += 1
            self._bump(accepted=1)
            return True

    def record_construction_drop(self) -> None:
        with self._lock:
            self._bump(dropped=1)

    def _run(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                with self._lock:
                    if self._state != "open" and self._pending == 0:
                        return
                continue
            try:
                self._sink.emit(event)
            except BaseException:
                with self._lock:
                    self._bump(sink_errors=1, dropped=1)
            else:
                with self._lock:
                    self._bump(emitted=1)
            finally:
                with self._lock:
                    self._pending -= 1
                    self._drained.notify_all()
                self._queue.task_done()

    def flush(self, timeout: float | None = None) -> bool:
        wait_for = self._close_timeout if timeout is None else max(0.0, timeout)
        deadline = time.monotonic() + wait_for
        with self._drained:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drained.wait(remaining)
            return True

    def close(self, timeout: float | None = None) -> bool:
        wait_for = self._close_timeout if timeout is None else max(0.0, timeout)
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                self._state = "closing"
        flushed = self.flush(wait_for)
        self._worker.join(max(0.0, wait_for))
        with self._lock:
            if not flushed or self._worker.is_alive():
                self._bump(close_timeouts=1)
                self._state = "closed"
                return False
            self._state = "closed"
        if isinstance(self._sink, CompositeSink):
            return self._sink.close(wait_for)
        return True

    @property
    def counters(self) -> EmitterCounters:
        with self._lock:
            return self._counters

    def diagnostics_snapshot(self) -> DiagnosticsSnapshotV1:
        with self._lock:
            lifecycle = self._state
            counters = self._counters.to_dict()
            depth = self._pending
        health = "degraded" if counters["sink_errors"] or counters["close_timeouts"] else "healthy"
        if lifecycle == "closed":
            health = "closed"
        return DiagnosticsSnapshotV1(
            sdk_version=__version__,
            lifecycle=lifecycle,
            health=health,
            counters=counters,
            queue_depth=depth,
            queue_capacity=self._capacity,
        )


class ObservabilityRuntime:
    """Public composition object retained by Host-facing runtimes."""

    def __init__(
        self,
        sink: ObservabilitySink | None = None,
        *,
        queue_capacity: int = 256,
        close_timeout: float = 1.0,
    ) -> None:
        self._emitter = SafeEmitter(
            sink,
            capacity=queue_capacity,
            close_timeout=close_timeout,
        )

    def emit(self, event: ObservabilityEventV1) -> bool:
        return self._emitter.emit(event)

    def emit_transition(
        self,
        event_name: str,
        *,
        component: str,
        operation: str,
        outcome: Outcome | str,
        correlation: CorrelationContext,
        attributes: Mapping[str, object] | None = None,
        severity: Severity | str = Severity.INFO,
        occurred_at: float | None = None,
    ) -> bool:
        """Construct and enqueue one event without affecting business code."""

        try:
            event = ObservabilityEventV1(
                event_name=event_name,
                occurred_at=time.time() if occurred_at is None else occurred_at,
                severity=Severity(severity),
                component=component,
                operation=operation,
                outcome=Outcome(outcome),
                correlation=correlation,
                attributes=cast(
                    Mapping[str, SafeValue], {} if attributes is None else attributes
                ),
            )
        except BaseException:
            self._emitter.record_construction_drop()
            return False
        return self._emitter.emit(event)

    def flush(self, timeout: float | None = None) -> bool:
        return self._emitter.flush(timeout)

    def close(self, timeout: float | None = None) -> bool:
        return self._emitter.close(timeout)

    @property
    def counters(self) -> EmitterCounters:
        return self._emitter.counters

    def diagnostics_snapshot(self) -> Mapping[str, object]:
        return self._emitter.diagnostics_snapshot().to_dict()


__all__ = ("EmitterCounters", "ObservabilityRuntime", "SafeEmitter")
