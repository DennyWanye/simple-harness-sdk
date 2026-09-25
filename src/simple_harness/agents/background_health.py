# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Health of the Agent runtime's background loops (2026-09-25 主流程优化条目 6).

The index pump, session draining, recall resumption and the built-in tool probe used
to swallow every error: a defect there stalled cleanup and recall forever and nobody
could see it.  Each loop now records its last failure here and logs it (rate limited);
the Host reads :meth:`BackgroundHealthBook.snapshot` into its status.  In-memory only —
a restart starts from a clean book.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Literal

Loop = Literal["index", "draining", "recall", "tool_probe", "reap"]
LOOPS: tuple[Loop, ...] = ("index", "draining", "recall", "tool_probe", "reap")

logger = logging.getLogger("simple_harness.agents.background")

#: One full stack trace per (loop, code) per this many milliseconds; the pump turns
#: every 50–250 ms, so an unlimited warning would flood the Host log.
LOG_INTERVAL_MS = 60_000


@dataclass(frozen=True, slots=True)
class BackgroundHealth:
    loop: Loop
    consecutive_failures: int = 0
    last_ok_at_ms: int | None = None
    last_error_at_ms: int | None = None
    last_error_code: str | None = None
    stuck_item: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "loop": self.loop,
            "consecutive_failures": self.consecutive_failures,
            "last_ok_at_ms": self.last_ok_at_ms,
            "last_error_at_ms": self.last_error_at_ms,
            "last_error_code": self.last_error_code,
            "stuck_item": self.stuck_item,
        }


def error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return str(code) if code else type(error).__name__


class BackgroundHealthBook:
    """Per-loop counters plus rate-limited logging.  Not thread-safe by design: every
    loop runs on the runtime's event loop."""

    def __init__(self, *, clock_ms=None) -> None:  # type: ignore[no-untyped-def]
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._rows: dict[str, BackgroundHealth] = {loop: BackgroundHealth(loop) for loop in LOOPS}
        self._last_logged: dict[tuple[str, str], int] = {}

    def ok(self, loop: Loop) -> None:
        self._rows[loop] = replace(self._rows[loop], consecutive_failures=0, last_ok_at_ms=self._clock_ms(),
                                   stuck_item=None)

    def fail(self, loop: Loop, error: BaseException, *, item: str | None = None) -> None:
        now = self._clock_ms()
        code = error_code(error)
        row = self._rows[loop]
        self._rows[loop] = replace(row, consecutive_failures=row.consecutive_failures + 1, last_error_at_ms=now,
                                   last_error_code=code, stuck_item=item)
        key = (loop, code)
        last = self._last_logged.get(key)
        if last is None or now - last >= LOG_INTERVAL_MS:
            self._last_logged[key] = now
            logger.warning("background loop %s failed (%s) item=%s consecutive=%d", loop, code, item,
                           row.consecutive_failures + 1, exc_info=error)

    def snapshot(self) -> tuple[BackgroundHealth, ...]:
        return tuple(self._rows[loop] for loop in LOOPS)


__all__ = ("BackgroundHealth", "BackgroundHealthBook", "LOOPS", "Loop", "error_code")
