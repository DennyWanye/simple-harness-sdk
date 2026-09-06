"""SDK-owned selection by the durable, validated start mode.

Selection exposes the actual driver to the kernel. It does not attest that a
Host driver is instrumented, and does not validate Host-control authorities.
Those checks remain with the selected Host-control implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from simple_harness.runtime.kernel import RuntimeDriver


@dataclass(frozen=True, slots=True)
class StartModeDriverRouter:
    ordinary: RuntimeDriver
    host_control: RuntimeDriver

    @property
    def policy_fingerprint(self) -> str | None:
        return getattr(self.ordinary, "policy_fingerprint", None)

    def select(self, start_mode: str) -> RuntimeDriver:
        if start_mode == "ordinary":
            return self.ordinary
        if start_mode == "host_control":
            return self.host_control
        raise ValueError("unsupported durable driver start mode")

    async def start(self, invocation, *, context, cancel):
        return await self.select(invocation.start.start_mode).start(
            invocation, context=context, cancel=cancel
        )
