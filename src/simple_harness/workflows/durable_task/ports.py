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

    def __init__(
        self, *, path: str, size: int, modified_at: float, checksum: str
    ) -> None:
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
    """LLM proposal generation with context."""

    async def generate_proposal(
        self,
        *,
        request: str,
        context: ProposalContext,
        capabilities: Sequence[CapabilityDescriptor],
        constraints: ProposalConstraints,
    ) -> ProposalResult:
        """Generate LLM proposal with available capabilities.

        Args:
            request: User request or task description
            context: Conversation context and history
            capabilities: Available capabilities for this proposal
            constraints: Generation constraints (tools, turns, approval)

        Returns:
            ProposalResult with content, tool calls, and convergence state
        """
        ...


class CapabilityCatalogPort(Protocol):
    """Capability search and binding."""

    async def search_capabilities(
        self,
        *,
        query: str,
        filters: CapabilityFilters,
    ) -> Sequence[CapabilityDescriptor]:
        """Search available capabilities.

        Args:
            query: Search query string
            filters: Additional filters (tags, categories)

        Returns:
            Sequence of matching capability descriptors
        """
        ...

    async def bind_capability(
        self,
        *,
        capability_id: str,
        generation: int,
    ) -> BoundCapability:
        """Bind specific capability version.

        Args:
            capability_id: Unique capability identifier
            generation: Capability generation number

        Returns:
            BoundCapability with handler and metadata

        Raises:
            ValueError: If capability not found or generation mismatch
        """
        ...


class WorkspacePort(Protocol):
    """Workspace file operations."""

    async def read_file(self, path: str) -> bytes:
        """Read file from workspace.

        Args:
            path: Relative path within workspace

        Returns:
            File contents as bytes

        Raises:
            FileNotFoundError: If file does not exist
        """
        ...

    async def write_file(self, path: str, content: bytes) -> WriteReceipt:
        """Write file to workspace with receipt.

        Args:
            path: Relative path within workspace
            content: File contents as bytes

        Returns:
            WriteReceipt with checksum and timestamp
        """
        ...

    async def list_files(self, pattern: str) -> Sequence[FileInfo]:
        """List files matching pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "src/**/*.ts")

        Returns:
            Sequence of FileInfo for matching files
        """
        ...


class ArtifactPort(Protocol):
    """Artifact creation and management."""

    async def create_artifact(
        self,
        *,
        name: str,
        content: bytes,
        metadata: Mapping[str, JsonValue],
    ) -> ArtifactId:
        """Create output artifact.

        Args:
            name: Artifact name
            content: Artifact content as bytes
            metadata: Additional metadata (type, description, etc.)

        Returns:
            ArtifactId with unique identifier and version
        """
        ...


class AuthorizationPort(Protocol):
    """Permission and authorization checks."""

    async def check_tool_authorization(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        context: AuthorizationContext,
    ) -> AuthorizationDecision:
        """Check if tool call is authorized.

        Args:
            tool_name: Name of the tool to check
            arguments: Tool call arguments
            context: Authorization context (run, user, session)

        Returns:
            AuthorizationDecision with allowed status and reason
        """
        ...


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
