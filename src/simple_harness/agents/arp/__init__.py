# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Native Agent Runtime Plane (ARP-EXEC-1.1.1) for BaseAgent / AgentRuntime / ReAct.

This package only *enhances* the existing runtime: durable creation intents, real
token metering and context manifests, one derived index partition per session with
lexical + vector retrieval and automatic recall, the unified capability / tool /
skill catalogue, session lifecycle with witnessed deletion, and the Host verbs,
events and error catalogue.  Contracts (schema, catalogues, SQL) are vendored under
``contracts/`` and ``sql/`` from ``plans/AgentRuntime/specs/1.1.1`` and are the
single field source; the creation protocol marker is ``ARP_V1_1_1``.
"""

from __future__ import annotations

PROTOCOL = "ARP_V1_1_1"
LEGACY_PROTOCOLS = ("LEGACY_AT_MIGRATION", "LEGACY_EXPLICIT", "ARP_V1", "ARP_V1_1")

__all__ = ("LEGACY_PROTOCOLS", "PROTOCOL")
