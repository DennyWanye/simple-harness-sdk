# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Memory port interfaces for optional Memory SDK integration.

These ports allow the Runtime to access long-term memory and working memory
without owning the storage implementation. Consumers can integrate their own
memory systems or a future standalone Memory SDK.
"""

from __future__ import annotations

from typing import Protocol

from simple_harness.contracts import JsonValue


class MemoryQueryPort(Protocol):
    """Read-only memory recall interface.

    Consumers implement this to give the Agent access to long-term memory
    without owning the storage. The Runtime calls this port when an Agent
    needs to recall relevant information from past conversations or knowledge.

    Example implementation:
        class MyMemoryQuery:
            async def recall_readonly(self, query, limit, scope):
                # Search your memory database
                results = await self.db.search(query, limit=limit, scope=scope)
                return [{"content": r.text, "timestamp": r.ts} for r in results]
    """

    async def recall_readonly(
        self,
        query: str,
        limit: int,
        scope: str,
    ) -> list[dict[str, JsonValue]]:
        """Return at most `limit` memory entries relevant to `query` within `scope`.

        Args:
            query: Natural language query describing what to recall
            limit: Maximum number of entries to return
            scope: Memory scope identifier (e.g., "user:123", "session:abc")

        Returns:
            List of memory entries as JSON-safe dictionaries. Each entry should
            contain at least a "content" field. Common fields include:
            - content: The memory text
            - timestamp: When the memory was created
            - relevance: Optional relevance score

        Raises:
            May raise exceptions on database errors. The Runtime will log and
            continue without memory augmentation.

        Constraints:
            - Must never write or mutate any state
            - Should return quickly (< 1 second for typical queries)
            - Empty list if no relevant memories found
        """
        ...


class MemoryWritePort(Protocol):
    """Write interface for session-scoped working memory.

    Consumers implement this to persist short-term notes and todos across turns.
    The Runtime calls this port when an Agent updates its working memory list.

    Example implementation:
        class MyMemoryWrite:
            async def replace_session_todos(self, session_id, items):
                # Overwrite the full todo list for this session
                await self.db.execute(
                    "DELETE FROM todos WHERE session_id = ?", (session_id,)
                )
                for item in items:
                    await self.db.execute(
                        "INSERT INTO todos (session_id, content) VALUES (?, ?)",
                        (session_id, item["content"])
                    )
    """

    async def replace_session_todos(
        self,
        session_id: str,
        items: list[dict[str, JsonValue]],
    ) -> None:
        """Replace the full working-memory list for `session_id`.

        Args:
            session_id: Execution session identifier
            items: New todo/note list as JSON-safe dictionaries. Each item
                   typically contains a "content" field describing the note.

        Raises:
            May raise exceptions on database errors. The Runtime will log the
            error and continue (working memory is non-critical).

        Constraints:
            - Should replace the entire list atomically (not append)
            - Previous items for this session should be removed
            - Empty list means clear all todos
            - Should complete quickly (< 500ms typical)
        """
        ...


__all__ = (
    "MemoryQueryPort",
    "MemoryWritePort",
)
