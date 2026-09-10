# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""BA35: the runtime-wide tool-call permit visible to the running tool handler.

A tool that parks on another Agent's progress (``agent_delegate`` waiting for its
child's result) must give its permit back before waiting, otherwise a cap of N
concurrent tool calls deadlocks as soon as N parents wait on children that need a
tool themselves (Slice 5 review C1).  The permit is released at most once.  Lives in ``tools`` (not
``agents``) so the context variable identity survives a re-import of the agents package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar


class ToolPermit:
    """One acquired slot of the tool semaphore; ``release`` is idempotent."""

    __slots__ = ("_on_release", "_released", "_semaphore")

    def __init__(self, semaphore: asyncio.Semaphore, on_release: Callable[[], None]) -> None:
        self._semaphore = semaphore
        self._on_release = on_release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._on_release()
        finally:
            self._semaphore.release()


_ACTIVE_PERMIT: ContextVar[ToolPermit | None] = ContextVar("base_agent_tool_permit", default=None)


def current_tool_permit() -> ToolPermit | None:
    """The permit held by the tool call running in this task, if any."""

    return _ACTIVE_PERMIT.get()


def release_tool_permit() -> bool:
    """Give the current tool permit back early; returns whether one was held."""

    permit = _ACTIVE_PERMIT.get()
    if permit is None or permit.released:
        return False
    permit.release()
    return True


__all__ = ("ToolPermit", "_ACTIVE_PERMIT", "current_tool_permit", "release_tool_permit")
