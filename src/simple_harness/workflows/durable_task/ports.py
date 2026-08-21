# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Port interfaces for durable task workflow capabilities.

These protocols define the boundary between SDK workflow logic and
Host-provided capabilities (LLM, tools, workspace, authorization).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from simple_harness.contracts import JsonValue


class ProposalContext:
    """Context for LLM proposal generation."""

    __slots__ = ("request", "plan_steps", "messages", "history")

    def __init__(
        self,
        *,
        request: str,
        plan_steps: Sequence[str],
        messages: Sequence[Mapping[str, JsonValue]],
        history: Sequence[Mapping[str, JsonValue]],
    ) -> None:
        self.request = request
        self.plan_steps = plan_steps
        self.messages = messages
        self.history = history


class ProposalConstraints:
    """Constraints for proposal generation."""

    __slots__ = ("max_turns", "allowed_tools", "forbidden_tools", "require_approval")

    def __init__(
        self,
        *,
        max_turns: int = 20,
        allowed_tools: frozenset[str] | None = None,
        forbidden_tools: frozenset[str] = frozenset(),
        require_approval: bool = True,
    ) -> None:
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.forbidden_tools = forbidden_tools
        self.require_approval = require_approval


class ProposalResult:
    """Result from LLM proposal generation."""

    __slots__ = ("content", "tool_calls", "convergence", "needs_clarification")

    def __init__(
        self,
        *,
        content: str,
        tool_calls: Sequence[Mapping[str, JsonValue]],
        convergence: str,
        needs_clarification: bool = False,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.convergence = convergence
        self.needs_clarification = needs_clarification


class CapabilityDescriptor:
    """Description of an available capability."""

    __slots__ = ("id", "name", "description", "generation", "fingerprint")

    def __init__(
        self,
        *,
        id: str,
        name: str,
        description: str,
        generation: int,
        fingerprint: str,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.generation = generation
        self.fingerprint = fingerprint


class BoundCapability:
    """A capability bound to specific version."""

    __slots__ = ("id", "generation", "handler", "metadata")

    def __init__(
        self,
        *,
        id: str,
        generation: int,
        handler: object,
        metadata: Mapping[str, JsonValue],
    ) -> None:
        self.id = id
        self.generation = generation
        self.handler = handler
        self.metadata = metadata


class CapabilityFilters:
    """Filters for capability search."""

    __slots__ = ("query", "tags", "categories")

    def __init__(
        self,
        *,
        query: str = "",
        tags: Sequence[str] = (),
        categories: Sequence[str] = (),
    ) -> None:
        self.query = query
        self.tags = tags
        self.categories = categories


class FileInfo:
    """Information about a workspace file."""

    __slots__ = ("path", "size", "modified_at", "checksum")

    def __init__(self, *, path: str, size: int, modified_at: float, checksum: str) -> None:
        self.path = path
        self.size = size
        self.modified_at = modified_at
        self.checksum = checksum


class WriteReceipt:
    """Receipt for file write operation."""

    __slots__ = ("path", "checksum", "timestamp")

    def __init__(self, *, path: str, checksum: str, timestamp: float) -> None:
        self.path = path
        self.checksum = checksum
        self.timestamp = timestamp


class ArtifactId:
    """Identifier for created artifact."""

    __slots__ = ("id", "version")

    def __init__(self, *, id: str, version: int) -> None:
        self.id = id
        self.version = version


class AuthorizationContext:
    """Context for authorization checks."""

    __slots__ = ("run_id", "request_id", "user_id", "session_metadata")

    def __init__(
        self,
        *,
        run_id: str,
        request_id: str,
        user_id: str,
        session_metadata: Mapping[str, JsonValue],
    ) -> None:
        self.run_id = run_id
        self.request_id = request_id
        self.user_id = user_id
        self.session_metadata = session_metadata


class AuthorizationDecision:
    """Decision from authorization check."""

    __slots__ = ("allowed", "reason", "requires_confirmation")

    def __init__(
        self, *, allowed: bool, reason: str = "", requires_confirmation: bool = False
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.requires_confirmation = requires_confirmation


class ProposalPort(Protocol):
    """Generate the next normalized proposal transition."""

    async def propose(self, proposal_state: object) -> object: ...

    async def propose_for_execution(
        self, proposal_state: object, *, execution_identity: object
    ) -> object: ...


class CapabilityCatalogPort(Protocol):
    """Read-only capability availability and lifecycle metadata."""

    async def is_capability_available(self, name: str) -> bool: ...

    async def get_capability_source(self, name: str) -> str | None: ...

    async def get_capability_policy(self, name: str) -> Mapping[str, JsonValue] | None: ...


class WorkspacePort(Protocol):
    """Dispatch prepared calls through the Host workspace/tool boundary."""

    async def execute_tools(
        self,
        calls: Sequence[object],
        *,
        workflow_step_id: str,
        prior_results: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class ArtifactPort(Protocol):
    """Optional evidence, testing, audit, and completion policy boundary."""

    async def check_completion_evidence(self, proposal_state: object, outcome: object) -> bool: ...

    async def completion_decision(
        self, decision: Mapping[str, JsonValue], proposal_state: object
    ) -> Mapping[str, JsonValue]: ...

    async def run_tests(self, proposal_state: object) -> Mapping[str, JsonValue]: ...

    async def audit(
        self, audit: Mapping[str, JsonValue], proposal_state: object
    ) -> Mapping[str, JsonValue]: ...


class AuthorizationPort(Protocol):
    """Commit an admission-issued authorization for one stable call."""

    async def grant_authorization(self, stable_call_id: str, authorization: object) -> None: ...


__all__ = [
    "ArtifactId",
    "ArtifactPort",
    "AuthorizationContext",
    "AuthorizationDecision",
    "AuthorizationPort",
    "BoundCapability",
    "CapabilityCatalogPort",
    "CapabilityDescriptor",
    "CapabilityFilters",
    "FileInfo",
    "ProposalConstraints",
    "ProposalContext",
    "ProposalPort",
    "ProposalResult",
    "WorkspacePort",
    "WriteReceipt",
]
