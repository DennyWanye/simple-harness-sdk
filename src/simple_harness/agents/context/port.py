# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``JournalContextPort``: the BaseAgent ``ContextPort`` (Slice 3).

* ``append`` writes one Journal row per message (never a rewritten snapshot,
  BA19); large tool results are stored whole with ``journal_only`` visibility
  and shown to the model as a preview plus a read-back reference (BA18).
* ``load`` assembles a bounded request from Journal units under the policy
  budget minus the run's tool schemas (BA13 / BA14 / BA15 / BA17) and records
  the selection (BA-v1.0 §7.4 step 9).  ``revision`` is the Journal high-water
  mark, so the ReAct loop's CAS discipline is unchanged.
* ``RequestGuard`` re-counts the final rendered request at the provider wire and
  refuses to send an over-budget request (BA16), binding the request identity to
  the selection.

Legacy Runs keep ``SqliteContextPort``; nothing here touches ``react_loop.py``.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from typing import cast

from simple_harness.contracts import JsonValue, RunId, canonical_json, thaw_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.base_agent import AgentJournalRecord
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import ProviderRequest, ProviderToolSpec
from simple_harness.providers.errors import ProviderRequestRejectedError
from simple_harness.runtime.context import ContextSnapshot, _message

from .budget import ContextPolicy, policy_hash
from .composer import assemble
from .protocol_groups import build_units
from .tokenizer import TokenizerPort, count_message, count_tools

ToolSpecsResolver = Callable[[str], tuple[ProviderToolSpec, ...]]


class ContextRequiredContentTooLarge(ProviderRequestRejectedError):
    """Required content alone exceeds the input budget: refused before any call (BA16)."""

    __slots__ = ("detail",)
    error_code = "context_required_content_too_large"
    default_message = "The required context exceeds the model's input budget."

    def __init__(self, *, detail: dict[str, JsonValue]) -> None:
        super().__init__()
        self.detail = detail


def _classify(append_id: str, run_id: str) -> tuple[str, str | None, str]:
    """(kind, turn_id, protocol_group_id) from the ReAct loop's append identities."""

    if append_id == f"{run_id}:context:instructions" or append_id == f"{run_id}:context:initial":
        return "instructions", None, "instructions"
    if append_id.endswith(":context:user") and ":input:" in append_id:
        turn_id = append_id[: -len(":context:user")]
        return "user_input", turn_id, append_id
    if append_id.endswith(":assistant"):
        return "assistant", None, append_id[: -len(":assistant")]
    if ":raw-call:" in append_id and ":turn:" in append_id:
        prefix, _, _ = append_id.partition(":raw-call:")
        ordinal = prefix.rsplit(":turn:", 1)[1]
        return "tool_result", None, f"{run_id}:provider-turn:{ordinal}"
    if append_id.endswith(":mandatory-context-feedback"):
        return "feedback", None, append_id[: -len(":mandatory-context-feedback")]
    return "feedback", None, append_id


class JournalContextPort:
    def __init__(
        self,
        uow: SqliteExecutionUnitOfWork,
        *,
        tokenizer: TokenizerPort,
        policy: ContextPolicy,
        model: str,
        tool_specs_for_run: ToolSpecsResolver,
        clock: Callable[[], float] = time.time,
        page_size: int = 64,
        recall: Callable[[str, str, int, tuple[int, ...]], Sequence[Message]] | None = None,
        recall_token_share: float = 0.0,
        on_records: Callable[[tuple[AgentJournalRecord, ...]], None] | None = None,
    ) -> None:
        self._uow = uow
        self._tokenizer = tokenizer
        self._policy = policy
        self._model = model
        self._tool_specs_for_run = tool_specs_for_run
        self._clock = clock
        self._page_size = max(8, int(page_size))
        # Slice 4: ``recall(agent_id, query, token_budget, exclude_seqs)`` returns
        # derived recall messages (never Journal rows, BA20); ``on_records`` feeds the
        # derived indexes after each committed append.
        self._recall = recall
        self._recall_share = recall_token_share
        self._on_records = on_records
        self.last_recall_query_hash: str | None = None
        self.policy_hash = policy_hash(
            policy, tokenizer_fingerprint=tokenizer.fingerprint, model=model
        )
        # Count cache keyed by content hash; the policy hash is part of the key so a
        # tokenizer / model / template change never reuses stale counts (BA22).
        self._counts: dict[tuple[str, str], int] = {}
        self.loads = 0
        self.records_read = 0

    # ---- counting -----------------------------------------------------------

    def _count(self, record: AgentJournalRecord) -> int:
        key = (self.policy_hash, record.content_hash)
        cached = self._counts.get(key)
        if cached is None:
            cached = count_message(self._tokenizer, _message(thaw_json(record.message_json)))
            self._counts[key] = cached
        if record.kind == "assistant":
            # The wire restores the assistant's issued tool calls from the effect
            # ledger before sending; count them here so the budget decision and the
            # final re-count see the same request (BA13).
            cached += self._tool_calls_overhead(record)
        return cached

    def _tool_calls_overhead(self, record: AgentJournalRecord) -> int:
        key = (self.policy_hash, f"group:{record.protocol_group_id}")
        cached = self._counts.get(key)
        if cached is not None:
            return cached
        prefix, _, ordinal = record.protocol_group_id.rpartition(":provider-turn:")
        if not ordinal.isdigit():
            return 0
        rows = self._uow.database.connection.execute(
            "SELECT raw_call_id, tool_name, arguments_json FROM execution_effects "
            "WHERE run_id=? AND turn_ordinal=? AND raw_call_id IS NOT NULL "
            "ORDER BY call_ordinal",
            (record.agent_id, int(ordinal)),
        ).fetchall()
        if not rows:
            return 0
        calls = [
            {"id": str(row[0]), "name": str(row[1]), "arguments": json.loads(str(row[2]) or "{}")}
            for row in rows
        ]
        overhead = self._tokenizer.count_text(canonical_json(cast(JsonValue, calls)))
        # Effects of a settled provider turn are immutable; an empty answer is not
        # cached because the group may still be in flight.
        self._counts[key] = overhead
        return overhead

    def tool_tokens(self, run_id: str) -> int:
        return count_tools(self._tokenizer, tuple(self._tool_specs_for_run(run_id)))

    # ---- ContextPort --------------------------------------------------------

    def revision(self, run_id: RunId) -> int:
        """Journal high-water mark without assembling (cheap CAS anchor for callers)."""

        return self._uow.agent_journal_highwater(run_id.value)

    def load(self, run_id: RunId) -> ContextSnapshot:
        return self._assemble(run_id, record=True)

    def _assemble(self, run_id: RunId, *, record: bool) -> ContextSnapshot:
        agent_id = run_id.value
        self.loads += 1
        highwater = self._uow.agent_journal_highwater(agent_id)
        if highwater == 0:
            return ContextSnapshot(0, ())
        budget = self._policy.input_budget()
        tool_tokens = self.tool_tokens(agent_id)
        available = max(0, budget - tool_tokens)
        # Bounded read: instructions plus the newest pages until the budget is spent.
        records = self._read_bounded(agent_id, highwater, available)
        units = build_units(records)
        # First pass with the whole budget; recall (S4) only takes room that the
        # required parts leave free, capped by its share, and only when it has hits.
        assembly = assemble(units, budget_tokens=available, count=self._count)
        current_turn = next(
            (unit.turn_id for unit in reversed(units) if unit.kind == "user_input"), None
        )
        messages = assembly.messages
        query_hash_value: str | None = None
        recall_cap = 0
        if self._recall is not None and not assembly.required_over_budget:
            recall_cap = min(
                int(available * self._recall_share), max(0, available - assembly.required_tokens)
            )
        if recall_cap > 0 and self._recall is not None:
            current_input = next(
                (unit for unit in reversed(units) if unit.kind == "user_input"), None
            )
            query = (
                ""
                if current_input is None
                else "\n".join(_text_of(r) for r in current_input.records)
            )
            if query.strip():
                recalled = tuple(self._recall(agent_id, query, recall_cap, assembly.selected_seqs))
                if recalled:
                    from ..memory.retrieval import query_hash as _qh

                    recall_tokens = sum(count_message(self._tokenizer, m) for m in recalled)
                    assembly = assemble(
                        units, budget_tokens=available - recall_tokens, count=self._count
                    )
                    query_hash_value = _qh(query.strip())
                    self.last_recall_query_hash = query_hash_value
                    # Recall goes after instructions/summary and before the recent units.
                    head = [m for m in assembly.messages if m.role is MessageRole.SYSTEM]
                    tail = [m for m in assembly.messages if m.role is not MessageRole.SYSTEM]
                    messages = tuple([*head, *recalled, *tail])
        if not record:
            return ContextSnapshot(highwater, messages)
        assembly_hash = hashlib.sha256(
            canonical_json(
                {
                    "seqs": list(assembly.selected_seqs),
                    "dropped": [list(r) for r in assembly.dropped_ranges],
                    "tools": tool_tokens,
                    "recall": query_hash_value,
                }
            ).encode("utf-8")
        ).hexdigest()
        selection_id = (
            f"{agent_id}:selection:{highwater}:{self.policy_hash[:12]}:{assembly_hash[:12]}"
        )
        self._uow.record_agent_context_selection(
            selection_id=selection_id,
            agent_id=agent_id,
            turn_id=current_turn,
            revision=highwater,
            source_highwater=highwater,
            selected_seqs=assembly.selected_seqs,
            dropped_ranges=assembly.dropped_ranges,
            required_over_budget=assembly.required_over_budget,
            message_tokens=assembly.message_tokens,
            tool_tokens=tool_tokens,
            budget_tokens=budget,
            policy_hash=self.policy_hash,
            tokenizer_fingerprint=self._tokenizer.fingerprint,
            now=self._clock(),
            query_hash=query_hash_value,
        )
        if assembly.summary is not None and assembly.summary_source_hash is not None:
            self._uow.upsert_agent_summary(
                agent_id=agent_id,
                scope="dropped_history",
                from_seq=assembly.dropped_ranges[0][0],
                to_seq=assembly.dropped_ranges[-1][1],
                source_hash=assembly.summary_source_hash,
                summary_json={
                    "text": assembly.summary.content,
                    "dropped_ranges": [list(r) for r in assembly.dropped_ranges],
                    "units": len(assembly.dropped_units),
                },
                generated_by="structural:v1",
                now=self._clock(),
            )
        return ContextSnapshot(highwater, messages)

    def _read_bounded(
        self, agent_id: str, highwater: int, available: int
    ) -> tuple[AgentJournalRecord, ...]:
        """Instructions + newest pages until their token sum exceeds the budget."""

        instructions = tuple(
            record
            for record in self._uow.read_agent_journal(
                agent_id, from_seq=1, to_seq=min(highwater, 8)
            )
            if record.kind == "instructions"
        )
        # The current input is fetched by identity, never inferred from whatever the
        # pages happened to return (review S3-02): the newest user_input row.
        current_input = self._uow.latest_agent_journal_record(agent_id, kind="user_input")
        collected: list[AgentJournalRecord] = []
        used = sum(self._count(record) for record in instructions)
        to_seq = highwater
        # Always take at least two pages so the tail group and the current input are
        # complete even when a single page is already over budget.
        pages = 0
        while to_seq >= 1:
            from_seq = max(1, to_seq - self._page_size + 1)
            page = self._uow.read_agent_journal(agent_id, from_seq=from_seq, to_seq=to_seq)
            self.records_read += len(page)
            collected[:0] = list(page)
            used += sum(self._count(record) for record in page if record.visibility == "context")
            pages += 1
            to_seq = from_seq - 1
            if used > available and pages >= 2:
                break
        # Group-aware boundary (review S3-07): extend backwards to the start of the
        # oldest collected protocol group so no unit is cut in half.
        if collected and to_seq >= 1:
            oldest = collected[0]
            if oldest.kind in ("assistant", "tool_result", "feedback"):
                head = self._uow.read_agent_journal_group_head(
                    agent_id, protocol_group_id=oldest.protocol_group_id, before_seq=oldest.seq
                )
                if head:
                    collected[:0] = list(head)
        covered = {record.seq for record in collected}
        merged = [record for record in instructions if record.seq not in covered] + collected
        if current_input is not None and current_input.seq not in covered:
            merged.append(current_input)
        return tuple(sorted(merged, key=lambda record: record.seq))

    def append(
        self,
        run_id: RunId,
        execution_lease: ExecutionLease,
        expected_revision: int,
        append_id: str,
        entries: Sequence[Message],
    ) -> ContextSnapshot:
        agent_id = run_id.value
        if not isinstance(append_id, str) or not append_id.strip():
            raise ValueError("append_id is required")
        items = tuple(entries)
        if not items or not all(isinstance(entry, Message) for entry in items):
            raise TypeError("entries must contain at least one Message")
        kind, turn_id, group_id = _classify(append_id, agent_id)
        payload: list[dict[str, JsonValue]] = [entry.to_dict() for entry in items]
        append_hash = hashlib.sha256(
            canonical_json(cast(JsonValue, payload)).encode("utf-8")
        ).hexdigest()
        rows: list[dict[str, JsonValue]] = []
        next_seq = expected_revision + 1
        for message, message_json in zip(items, payload):
            provenance = {
                "instructions": "input",
                "user_input": "input",
                "assistant": "model",
                "tool_result": "ledger",
                "feedback": "derived",
            }[kind]
            if kind == "tool_result" and self._tool_result_is_large(message):
                full_offset = len(rows)
                rows.append(
                    {
                        "message_json": message_json,
                        "kind": kind,
                        "turn_id": turn_id,
                        "protocol_group_id": group_id,
                        "provenance": provenance,
                        "visibility": "journal_only",
                        "full_record_offset": None,
                    }
                )
                preview = self._preview_of(message, full_seq=next_seq + full_offset)
                rows.append(
                    {
                        "message_json": preview.to_dict(),
                        "kind": kind,
                        "turn_id": turn_id,
                        "protocol_group_id": group_id,
                        "provenance": provenance,
                        "visibility": "context",
                        "full_record_offset": full_offset,
                    }
                )
                continue
            rows.append(
                {
                    "message_json": message_json,
                    "kind": kind,
                    "turn_id": turn_id,
                    "protocol_group_id": group_id,
                    "provenance": provenance,
                    "visibility": "context",
                    "full_record_offset": None,
                }
            )
        stored, created, highwater = self._uow.append_agent_journal(
            agent_id=agent_id,
            append_id=append_id,
            append_hash=append_hash,
            expected_highwater=expected_revision,
            entries=rows,
            execution_lease=execution_lease,
            now=self._clock(),
        )
        if created and self._on_records is not None:
            self._on_records(stored)
        # A snapshot for the caller's CAS chain (every ReAct append site reads only
        # ``revision``); selections are recorded only by ``load``, never by appends.
        return ContextSnapshot(highwater, ())

    # ---- large tool results --------------------------------------------------

    def _tool_result_is_large(self, message: Message) -> bool:
        return count_message(self._tokenizer, message) > self._policy.max_tool_result_tokens

    def _preview_of(self, message: Message, *, full_seq: int) -> Message:
        content = (
            message.content
            if isinstance(message.content, str)
            else canonical_json([block.to_dict() for block in message.content])
        )
        try:
            payload = json.loads(content)
        except ValueError:
            payload = {"value": content}
        limit = self._policy.tool_result_preview_chars
        if isinstance(payload, dict):
            value_text = canonical_json(payload.get("value"))
            preview_payload: dict[str, JsonValue] = {
                key: payload[key]
                for key in ("outcome", "error_code", "public_message")
                if key in payload
            }
            preview_payload["value_preview"] = value_text[:limit]
        else:
            preview_payload = {"value_preview": content[:limit]}
        preview_payload["truncated"] = True
        preview_payload["journal_read_back"] = {"seq": full_seq, "tool": "session_history.read"}
        return Message(
            message.role,
            canonical_json(preview_payload),
            name=message.name,
            call_id=message.call_id,
            metadata={**dict(message.metadata), "journal_full_record_seq": full_seq},
        )


class RequestGuard:
    """Final re-count on the rendered request at the provider wire (BA13 / BA16)."""

    def __init__(
        self,
        uow: SqliteExecutionUnitOfWork,
        *,
        tokenizer: TokenizerPort,
        policy: ContextPolicy,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._uow = uow
        self._tokenizer = tokenizer
        self._policy = policy
        self._clock = clock
        self.last_request_tokens: int | None = None

    def count(self, request: ProviderRequest) -> int:
        return sum(count_message(self._tokenizer, m) for m in request.messages) + count_tools(
            self._tokenizer, tuple(request.tools)
        )

    def check(self, request: ProviderRequest, *, run_id: str) -> None:
        tokens = self.count(request)
        self.last_request_tokens = tokens
        budget = self._policy.input_budget()
        selection = self._uow.latest_agent_context_selection(run_id)
        if selection is not None and selection.provider_request_id is None:
            from simple_harness.execution.provider_invocations import provider_request_fingerprint

            self._uow.bind_agent_context_selection_request(
                selection_id=selection.selection_id,
                provider_request_id=request.request_id.value,
                request_hash=provider_request_fingerprint(request),
                request_tokens=tokens,
                now=self._clock(),
            )
        if tokens > budget + self._policy.render_slack_tokens or (
            selection is not None and selection.required_over_budget
        ):
            raise ContextRequiredContentTooLarge(
                detail={
                    "request_tokens": tokens,
                    "budget_tokens": budget,
                    "required_over_budget": bool(
                        selection is not None and selection.required_over_budget
                    ),
                    "tokenizer": self._tokenizer.fingerprint,
                }
            )


def _text_of(record: AgentJournalRecord) -> str:
    message = thaw_json(record.message_json)
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else canonical_json(content)


__all__ = (
    "ContextRequiredContentTooLarge",
    "JournalContextPort",
    "RequestGuard",
    "ToolSpecsResolver",
)
