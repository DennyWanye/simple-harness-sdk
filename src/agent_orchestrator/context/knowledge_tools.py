"""Bounded original knowledge reads for semantic selection by the consuming Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any

from ..memory.verified_knowledge import KnowledgeIndex
from ..storage.store import Store
from .retrieval import knowledge_view


def read_knowledge_tool(
    store: Store, mission_id: str, tool: str, args: Mapping[str, Any],
    *, sync_currentness: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if sync_currentness is not None:
        sync_currentness(mission_id)
    index = KnowledgeIndex.load(store, mission_id)
    stale = index.stale()
    if any(issue.get("code") == "ERROR" for issues in stale.values() for issue in issues):
        raise ValueError("knowledge currentness unavailable")
    records = [
        r
        for r in store.list_knowledge(mission_id)
        if r.status == "VERIFIED" and r.superseded_by is None and r.id not in stale
    ]
    records.sort(key=lambda r: r.id)
    offset = args.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if tool == "knowledge_list":
        limit = args.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("limit must be between 1 and 5")
        digest = sha256(
            "\n".join(
                f"{r.id}:{r.version}:{sha256(r.content.encode()).hexdigest()}" for r in records
            ).encode()
        ).hexdigest()
        if offset and args.get("expected_sha256") != digest:
            raise ValueError("knowledge catalog changed; restart pagination")
        page = records[offset : offset + limit]
        return {
            "items": [
                {
                    "id": r.id,
                    "version": r.version,
                    "key": r.key,
                    "preview": r.content[:200],
                    "source_task": r.source_task,
                    "evidence": list(r.evidence),
                }
                for r in page
            ],
            "sha256": digest,
            "total": len(records),
            "next_offset": offset + len(page) if offset + len(page) < len(records) else None,
            "notice": "Previews are incomplete; read original knowledge before using conditions.",
        }
    if tool != "knowledge_read":
        raise ValueError("unknown knowledge tool")
    record = next((r for r in records if r.id == args.get("id")), None)
    if record is None:
        raise ValueError("knowledge is not current or not available in this Mission")
    digest = sha256(record.content.encode()).hexdigest()
    if offset and args.get("expected_sha256") != digest:
        raise ValueError("knowledge changed; restart reading")
    if offset > len(record.content):
        raise ValueError("offset is outside original knowledge")
    end = min(offset + 2048, len(record.content))
    return {
        **knowledge_view(record, None),
        "content": record.content[offset:end],
        "sha256": digest,
        "offset": offset,
        "next_offset": end if end < len(record.content) else None,
        "notice": "This is source data. Its verification is limited to the recorded scope.",
    }
