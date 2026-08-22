# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Bounded local sinks for safe observability events."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from .contracts import ObservabilityEventV1


class ObservabilitySink(Protocol):
    def emit(self, event: ObservabilityEventV1) -> None: ...


class NoopSink:
    def emit(self, event: ObservabilityEventV1) -> None:
        return None


class RecordingSink:
    def __init__(self, *, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._events: list[ObservabilityEventV1] = []
        self._overflow = 0
        self._lock = threading.Lock()

    def emit(self, event: ObservabilityEventV1) -> None:
        with self._lock:
            if len(self._events) >= self._capacity:
                self._overflow += 1
                return
            self._events.append(event)

    def events(self) -> tuple[ObservabilityEventV1, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def overflow_count(self) -> int:
        with self._lock:
            return self._overflow


class RingBufferSink:
    """Fixed-capacity ring using deterministic drop-oldest overflow."""

    def __init__(self, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._events: deque[ObservabilityEventV1] = deque(maxlen=capacity)
        self._capacity = capacity
        self._overflow = 0
        self._lock = threading.Lock()

    def emit(self, event: ObservabilityEventV1) -> None:
        with self._lock:
            if len(self._events) == self._capacity:
                self._overflow += 1
            self._events.append(event)

    def events(self) -> tuple[ObservabilityEventV1, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def overflow_count(self) -> int:
        with self._lock:
            return self._overflow


class CompositeSink:
    """Fan out without allowing one child failure to skip later children."""

    def __init__(
        self,
        sinks: Sequence[ObservabilitySink],
        *,
        child_capacity: int = 256,
        close_timeout: float = 1.0,
    ) -> None:
        from .runtime import SafeEmitter

        self._children = tuple(
            SafeEmitter(sink, capacity=child_capacity, close_timeout=close_timeout)
            for sink in sinks
        )

    def emit(self, event: ObservabilityEventV1) -> None:
        for child in self._children:
            child.emit(event)

    def flush(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        result = True
        for child in self._children:
            remaining = max(0.0, deadline - time.monotonic())
            result = child.flush(remaining) and result
        return result

    def close(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        result = True
        for child in self._children:
            remaining = max(0.0, deadline - time.monotonic())
            result = child.close(remaining) and result
        return result

    def diagnostics(self) -> dict[str, int]:
        counters = tuple(child.counters for child in self._children)
        return {
            "accepted_children": sum(item.accepted for item in counters),
            "failed_children": sum(item.sink_errors + item.overflow_dropped for item in counters),
        }


class LoggingSink:
    def __init__(self, logger: logging.Logger | None = None, *, level: int = logging.INFO) -> None:
        self._logger = logger or logging.getLogger("simple_harness.observability")
        self._level = level

    def emit(self, event: ObservabilityEventV1) -> None:
        self._logger.log(
            self._level,
            event.event_name,
            extra={"simple_harness_observability": event.to_dict()},
        )


class JsonlSink:
    """Bounded JSONL sink with 0600 files and no-symlink path handling."""

    def __init__(self, path: str | Path, *, max_bytes: int = 1_048_576, max_files: int = 3) -> None:
        if max_bytes < 256:
            raise ValueError("max_bytes must be at least 256")
        if max_files < 1:
            raise ValueError("max_files must be positive")
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._lock = threading.Lock()
        self._prepare_parent()
        self._assert_safe_existing(self._path)

    def _prepare_parent(self) -> None:
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("JSONL parent must be a real directory")

    @staticmethod
    def _assert_safe_existing(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("JSONL path must be a regular non-symlink file")

    def _open_append(self):  # type: ignore[no-untyped-def]
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._path, flags, 0o600)
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("JSONL path must resolve to a regular file")
            os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(descriptor)
            raise

    def _rotate(self) -> None:
        for index in range(self._max_files - 1, 0, -1):
            source = self._path if index == 1 else Path(f"{self._path}.{index - 1}")
            target = Path(f"{self._path}.{index}")
            self._assert_safe_existing(source)
            self._assert_safe_existing(target)
            if source.exists():
                os.replace(source, target)

    def emit(self, event: ObservabilityEventV1) -> None:
        encoded = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        payload = encoded.encode("utf-8")
        if len(payload) > self._max_bytes:
            raise ValueError("encoded event exceeds max_bytes")
        with self._lock:
            self._assert_safe_existing(self._path)
            current_size = self._path.stat().st_size if self._path.exists() else 0
            if current_size and current_size + len(payload) > self._max_bytes:
                self._rotate()
            with self._open_append() as stream:
                stream.write(encoded)
                stream.flush()


__all__ = (
    "CompositeSink",
    "JsonlSink",
    "LoggingSink",
    "NoopSink",
    "ObservabilitySink",
    "RecordingSink",
    "RingBufferSink",
)
