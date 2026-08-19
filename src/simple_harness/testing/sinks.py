# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Test-only delivery sinks."""

from __future__ import annotations

from collections.abc import Mapping

from simple_harness.contracts import JsonValue


class NoopDeliverySink:
    """Delivery sink that silently accepts and discards every delivery.

    TEST ONLY — do not use in production. ``DeliveryDispatcher`` records a
    delivery as DELIVERED whenever the sink returns without raising, so this
    sink fabricates successful delivery. It exists only for tests and examples
    that need an explicit no-op sink.
    """

    async def deliver(
        self, payload: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> None:
        return None


__all__ = ("NoopDeliverySink",)
