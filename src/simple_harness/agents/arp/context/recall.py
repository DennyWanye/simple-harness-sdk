# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``ContextRecallCoordinator``: automatic F for one prepared request (CONTEXT-RECALL.md, R3).

One ``ContextRecallRequest`` per original request key; the central row
(``arp_context_recalls``) holds only coordination (a ``ContextRecallCheckpoint``
and, at the end, an immutable ``ContextRecallResult``).  The stages:

* C0 ``start`` freezes the request and a PREPARING checkpoint (replays return the
  existing row; another body under the same key is ``RECALL_BINDING_INVALID``);
* C1 an ``IndexSnapshot`` is frozen in the partition (upper_commit, highwater,
  exclusions, authority read set);
* C2/C3 the query embedding is obtained **once** under the call key
  ``recall_key/query/<embedding_fingerprint>`` (a persisted successful call is
  reused, a failure degrades to LEXICAL_ONLY only when the policy allows it,
  otherwise the recall is BLOCKED);
* C4/C5 SCANNING then RESULTS pages of the same frozen query are collected and
  aggregated (page chain, ranks, totals verified);
* C6 the result is written READY / SKIPPED; the composer only ever sees an
  aggregate, never a page in flight.

Pages are advanced inline within the request's time budget; a process exit
between pages resumes from the checkpoint (same key, same query, same cursor).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .. import embedding_call, store
from ..codec import check
from ..errors import ArpError
from ..partition import NO_EMBEDDING_FINGERPRINT
from ..pins import Pin
from ..retrieval_status import status_for
from ..search import SessionAccess, SessionSearchService
from ..strict import canonical, digest, plain

TRANSIENT_CODES = frozenset({"FILE_BUSY"})
TERMINAL_CODES = frozenset(
    {"RECALL_BINDING_INVALID", "RECALL_SOURCE_STALE", "RECALL_PREPARE_BLOCKED", "EMBEDDING_UNAVAILABLE",
     "RECALL_AGGREGATE_MISMATCH", "CURSOR_EXPIRED", "CLOCK_ROLLBACK", "SESSION_NOT_ACTIVE"}
)

QUERY_PART_LIMITS = {"CURRENT_INPUT": (1536, 512), "TASK_GOAL": (1024, 0), "LATEST_FEEDBACK": (768, 0)}
QUERY_RECIPE = "context-query-v1"
MAX_CANDIDATE_BYTES = 2_097_152
MAX_SCAN_PAGES = 256
MAX_RESULT_PAGES = 16
EMBEDDING_CALL_KIND = "embedding_call"


def recall_key_for(root_incarnation: str, session_id: str, original_request_key: str) -> str:
    return digest(
        {
            "kind": "context-recall-key-v1",
            "root_incarnation": root_incarnation,
            "session_id": session_id,
            "original_request_key": original_request_key,
        }
    )


def build_query(parts: Mapping[str, tuple[str, Pin]]) -> tuple[str, list[dict[str, Any]]]:
    """``context-query-v1``: stable slot order, empty slots omitted, bounded slicing."""

    unknown = set(parts) - set(QUERY_PART_LIMITS)
    if unknown:
        raise ArpError("RECALL_BINDING_INVALID", "unknown query part kind")
    chunks: list[str] = []
    coverage: list[dict[str, Any]] = []
    for kind, (head, tail) in QUERY_PART_LIMITS.items():
        if kind not in parts:
            continue
        raw, source_ref = parts[kind]
        if not isinstance(raw, str):
            raise ArpError("RECALL_BINDING_INVALID", "query part must be text")
        normalized = " ".join(raw.split())
        if not normalized:
            continue
        if len(normalized) <= head + tail:
            selected, used_head, used_tail, truncated = normalized, len(normalized), 0, False
        else:
            selected = normalized[:head] + (" … " + normalized[-tail:] if tail else "")
            used_head, used_tail, truncated = head, tail, True
        chunks.append(f"[{kind}]\n{selected}")
        coverage.append(
            {
                "source_kind": kind,
                "source_ref": source_ref.to_json(),
                "head_chars": used_head,
                "tail_chars": used_tail,
                "text_hash": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
                "truncated": truncated,
            }
        )
    query = "\n".join(chunks)
    if len(query) > 4096 or len(query.encode("utf-8")) > 16384:
        raise ArpError("RECALL_QUERY_LIMIT")
    return query, coverage


def limits_for(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "page_rows": int(policy["max_scan_rows_per_page"]),
        "page_budget_ms": int(policy["max_retrieval_ms"]),
        "total_budget_ms": int(policy["max_query_total_ms"]),
        "max_scan_pages": MAX_SCAN_PAGES,
        "max_result_pages": MAX_RESULT_PAGES,
        "channel_top_k": int(policy["max_query_candidates"]),
        "global_candidates": int(policy["max_candidates"]),
        "max_candidate_bytes": MAX_CANDIDATE_BYTES,
        "recall_token_ceiling": int(policy["recall_max_tokens"]),
        "max_recall_items": int(policy["max_recall_items"]),
    }


@dataclass(frozen=True, slots=True)
class RecallSources:
    """Everything the composer captured for one prepare slot (all frozen before C0)."""

    access: SessionAccess
    session: store.SessionRow
    turn_id: str
    original_request_key: str
    provider_request_ordinal: int
    policy_ref: Pin
    adoption_revision: int
    source_snapshot_ref: Pin
    source_snapshot_hash: str
    group_snapshot_ref: Pin
    journal_highwater: int
    expected_group_set_hash: str
    expected_groups: int
    mandatory_group_ids: tuple[str, ...]
    protected_group_ids: tuple[str, ...]
    query_parts: Mapping[str, tuple[str, Pin]]
    embedding_resource_ref: Pin | None
    embedding_fingerprint: str | None
    allow_lexical_degradation: bool
    limits: Mapping[str, Any]
    clock_receipt_ref: Pin


@dataclass(slots=True)
class ContextRecallCoordinator:
    uow: Any
    search_for: Callable[[store.SessionRow], tuple[SessionSearchService, Any, Any]]  # (service, guard, generation)
    embedding: Any | None
    clock_ms: Callable[[], int]
    clock: Callable[[], float]
    fault: Callable[[str], None] | None = None
    embedding_deployment_ref: Pin | None = None
    capture: Callable[[store.SessionRow, int], Any] | None = None  # (session, highwater) -> GroupSnapshot
    _pages: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)

    # ---- request construction --------------------------------------------------------

    def build_request(self, sources: RecallSources) -> dict[str, Any]:
        query_text, parts = build_query(sources.query_parts)
        created = self.clock_ms()
        mandatory = sorted(set(sources.mandatory_group_ids))
        protected = sorted(set(sources.protected_group_ids) - set(mandatory))
        request = {
            "schema_version": 1,
            "recall_key": recall_key_for(sources.access.root_incarnation, sources.session.session_id, sources.original_request_key),
            "original_request_key": sources.original_request_key,
            "agent_id": sources.session.agent_id,
            "session_id": sources.session.session_id,
            "turn_id": sources.turn_id,
            "provider_request_ordinal": sources.provider_request_ordinal,
            "root_incarnation": sources.access.root_incarnation,
            "control_generation": sources.session.generation,
            "policy_ref": sources.policy_ref.to_json(),
            "adoption_revision": sources.adoption_revision,
            "source_snapshot_ref": sources.source_snapshot_ref.to_json(),
            "source_snapshot_hash": sources.source_snapshot_hash,
            "group_snapshot_ref": sources.group_snapshot_ref.to_json(),
            "journal_highwater": sources.journal_highwater,
            "expected_group_set_hash": sources.expected_group_set_hash,
            "authority_readset_hash": sources.access.authority_readset_hash,
            "mandatory_group_ids": mandatory,
            "protected_group_ids": protected,
            "exclusions_hash": digest(sorted(set(mandatory) | set(protected))),
            "query_recipe": QUERY_RECIPE,
            "query_text": query_text,
            "query_hash": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
            "query_parts": parts,
            "embedding_resource_ref": None if sources.embedding_resource_ref is None else sources.embedding_resource_ref.to_json(),
            "embedding_fingerprint": sources.embedding_fingerprint,
            "allow_lexical_degradation": sources.allow_lexical_degradation,
            "limits": dict(sources.limits),
            "created_at_ms": created,
            "deadline_ms": created + int(sources.limits["total_budget_ms"]),
            "clock_receipt_ref": sources.clock_receipt_ref.to_json(),
        }
        return check("ContextRecallRequest", request)

    # ---- C0 -------------------------------------------------------------------------------

    def start(self, access: SessionAccess, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """C0: freeze the request once; a re-sent key must carry the same body."""

        value = check("ContextRecallRequest", dict(request))
        with self.uow.database.transaction() as connection:
            existing = store.get_context_recall_exact(connection, value["original_request_key"])
            if existing is not None and existing.request_hash != digest(value):
                raise ArpError("RECALL_BINDING_INVALID", "request key re-sent with another body")
            if existing is None:
                now = self.clock_ms()
                checkpoint = {
                    "schema_version": 1,
                    "recall_key": value["recall_key"],
                    "request_hash": digest(value),
                    "phase": "PREPARING",
                    "query_id": f"{value['recall_key']}:query",
                    "index_snapshot_ref": None,
                    "mode": None,
                    "query_call_key": None,
                    "embedding_invocation_ref": None,
                    "cursor_token": None,
                    "page_refs": [],
                    "scan_pages_committed": 0,
                    "result_pages_committed": 0,
                    "scanned_chunks": 0,
                    "snapshot_chunks": 0,
                    "next_result_offset": 0,
                    "created_at_ms": int(value["created_at_ms"]),
                    "deadline_ms": int(value["deadline_ms"]),
                    "last_observed_at_ms": max(now, int(value["created_at_ms"])),
                    "next_wake_at_ms": min(int(value["deadline_ms"]), max(now, int(value["created_at_ms"]))),
                    "last_clock_receipt_ref": value["clock_receipt_ref"],
                    "terminal_error_code": None,
                }
                row = store.put_context_recall_locked(connection, request=value, checkpoint=checkpoint)
            else:
                row = existing
        self._fault_point("recall.after_c0")
        return self.resume(row.recall_key, access, now_ms=self.clock_ms(), clock_receipt_ref=Pin.from_json(value["clock_receipt_ref"]))

    # ---- resume / drive ------------------------------------------------------------------

    def resume(self, recall_key: str, access: SessionAccess, *, now_ms: int, clock_receipt_ref: Pin) -> Mapping[str, Any]:
        """Drive the row to a terminal phase. Every non-transient failure lands as BLOCKED/STALE
        on the row itself (with a named code); only ``FILE_BUSY`` escapes, to be retried."""

        row = store.read_context_recall(self.uow.database.connection, recall_key)
        if row is None:
            raise ArpError("RECALL_BINDING_INVALID", "unknown recall key")
        if row.session_id != access.session_id or row.agent_id != access.agent_id:
            raise ArpError("RECALL_SOURCE_STALE", "access differs from the coordinated request")
        if row.control_generation != access.control_generation and not row.terminal:
            # The Session was fenced (destroy / quarantine) after this recall was coordinated:
            # the row records STALE by name (J7) instead of failing every later pass.
            row = self._block(row, ArpError("RECALL_SOURCE_STALE", "session generation advanced"), clock_receipt_ref=clock_receipt_ref, now_ms=max(now_ms, self.clock_ms()))
        while not row.terminal:
            try:
                row = self._step(row, access, now_ms=max(now_ms, self.clock_ms()), clock_receipt_ref=clock_receipt_ref)
            except ArpError as error:
                if error.code in TRANSIENT_CODES:
                    raise
                row = self._block(row, error, clock_receipt_ref=clock_receipt_ref, now_ms=max(now_ms, self.clock_ms()))
        if row.result is not None:
            return row.result
        raise ArpError(
            "RECALL_PREPARE_BLOCKED",
            f"recall {row.phase}",
            detail={"terminal_error_code": row.checkpoint.get("terminal_error_code")},
        )

    def _block(self, row: store.ContextRecallRow, error: ArpError, *, clock_receipt_ref: Pin, now_ms: int) -> store.ContextRecallRow:
        """Record a named terminal failure; the original code and detail go to an audit receipt."""

        current = store.read_context_recall(self.uow.database.connection, row.recall_key) or row
        if current.terminal:
            return current
        with self.uow.database.transaction() as txn:
            store.append_original_receipt_locked(
                txn, run_id=current.agent_id, kind="recall_block", receipt_key=f"{current.recall_key}:{current.row_version}",
                body={"phase": current.phase, "code": error.code, "field_path": error.field_path, "detail": plain(error.detail), "message": str(error)},
                now=self.clock(),
            )
        point = dict(current.checkpoint)
        point["last_observed_at_ms"] = max(now_ms, int(point["last_observed_at_ms"]))
        point["last_clock_receipt_ref"] = clock_receipt_ref.to_json()
        phase = "STALE" if error.code in ("RECALL_SOURCE_STALE", "CURSOR_STALE", "SESSION_NOT_ACTIVE") else "BLOCKED"
        code = error.code if error.code in TERMINAL_CODES else "RECALL_PREPARE_BLOCKED"
        return self._terminal(current, point, phase, code)

    def _expected_groups(self, session: store.SessionRow, request: Mapping[str, Any]) -> int | None:
        """Re-derive the frozen group set from the journal at the request's highwater (never a call
        parameter); ``None`` when the journal no longer yields the same set (the request is stale)."""

        if self.capture is None:
            raise ArpError("STATE_COMBINATION_INVALID", "recall coordinator has no group capture")
        snapshot = self.capture(session, int(request["journal_highwater"]))
        if snapshot.expected_group_set_hash != request["expected_group_set_hash"]:
            return None
        return sum(1 for g in snapshot.groups if g.indexable)

    def _step(self, row: store.ContextRecallRow, access: SessionAccess, *, now_ms: int, clock_receipt_ref: Pin) -> store.ContextRecallRow:
        request = row.request
        point = dict(row.checkpoint)
        point["last_observed_at_ms"] = max(now_ms, int(point["last_observed_at_ms"]))
        point["last_clock_receipt_ref"] = clock_receipt_ref.to_json()
        session = store.read_session(self.uow.database.connection, row.session_id)
        if session is None or session.state != "ACTIVE" or session.generation != row.control_generation:
            return self._terminal(row, point, "STALE", "RECALL_SOURCE_STALE")
        limits = request["limits"]
        # Deadline / page caps: only checked before starting more work.
        if now_ms >= row.deadline_ms or int(point["scan_pages_committed"]) >= MAX_SCAN_PAGES:
            if point["phase"] == "WAITING_EMBEDDING" and not request["allow_lexical_degradation"]:
                return self._terminal(row, point, "BLOCKED", "EMBEDDING_UNAVAILABLE")
            return self._skipped(row, point, "INDEX_SCAN_LIMIT", access)
        if point["phase"] == "PREPARING":
            if not request["query_text"]:
                return self._skipped(row, point, "EMPTY_QUERY", access)
            if int(limits["recall_token_ceiling"]) == 0 or int(limits["max_recall_items"]) == 0:
                return self._skipped(row, point, "RECALL_DISABLED", access)
            expected_groups = self._expected_groups(session, request)
            if expected_groups is None:
                return self._terminal(row, point, "STALE", "RECALL_SOURCE_STALE")
            service, guard, generation = self.search_for(session)
            with guard.held():
                snapshot = service.freeze_snapshot(
                    access,
                    index_generation=generation.index_generation,
                    journal_highwater=int(request["journal_highwater"]),
                    expected_group_set_hash=request["expected_group_set_hash"],
                    expected_groups=expected_groups,
                    source_snapshot_hash=request["source_snapshot_hash"],
                    embedding_fingerprint=generation.embedding_fingerprint,
                    chunker_fingerprint=generation.chunker_fingerprint,
                    view_policy_hash=generation.view_policy_hash,
                    identity={"recall_key": row.recall_key, "query": request["query_hash"], "exclusions": request["exclusions_hash"]},
                )
            point["index_snapshot_ref"] = Pin("index_snapshot", snapshot["snapshot_id"], int(snapshot["index_generation"]), digest(snapshot)).to_json()
            point["snapshot_chunks"] = 0
            if request["embedding_fingerprint"] is None or generation.embedding_fingerprint == NO_EMBEDDING_FINGERPRINT:
                point["mode"] = "LEXICAL_ONLY"
                point["phase"] = "SCANNING"
            else:
                point["phase"] = "WAITING_EMBEDDING"
                point["query_call_key"] = f"{row.recall_key}/query/{request['embedding_fingerprint']}"
            return self._cas(row, point)
        if point["phase"] == "WAITING_EMBEDDING":
            try:
                receipt = self._embed_query(row, point, request)
            except ArpError as error:
                if error.code != "EMBEDDING_UNAVAILABLE" or not request["allow_lexical_degradation"]:
                    raise
                receipt = {"status": "FAILED", "invocation_ref": None}
            if receipt["status"] == "SUCCEEDED":
                point["mode"] = "HYBRID"
            elif request["allow_lexical_degradation"]:
                point["mode"] = "LEXICAL_ONLY"
            else:
                return self._terminal(row, point, "BLOCKED", "EMBEDDING_UNAVAILABLE")
            point["embedding_invocation_ref"] = receipt["invocation_ref"]
            point["phase"] = "SCANNING"
            return self._cas(row, point)
        # SCANNING / FETCHING_RESULTS: one page per step.
        service, guard, generation = self.search_for(session)
        snapshot_id = str(point["index_snapshot_ref"]["id"])
        pages = self._pages.setdefault(row.recall_key, [])
        if point["phase"] == "FETCHING_RESULTS" and point["cursor_token"] is None:
            # Every page of the frozen query is collected: aggregate (C5) and freeze (C6).
            with guard.held():
                snapshot = service.snapshot_extras(snapshot_id)
            if len(pages) != len(point["page_refs"]):
                pages[:] = self._reload_pages(service, point)
            return self._ready(row, point, access, snapshot, pages)
        with guard.held():
            snapshot = service.snapshot_extras(snapshot_id)
            if int(snapshot["index_generation"]) != generation.index_generation:
                return self._terminal(row, point, "STALE", "CURSOR_STALE")
            if point["cursor_token"] is None:
                vector, vector_ref = self._query_vector(row, point)
                page, cursor = service.start_frozen(
                    access, request, snapshot, query_vector=vector, query_vector_ref=vector_ref, mode=str(point["mode"]), query_id=row.query_id
                )
            else:
                page, cursor = service.resume_frozen(access, str(point["cursor_token"]))
        self._fault_point("recall.after_page")
        pages.append(page)
        ordinal = len(point["page_refs"]) + 1
        page_ref = Pin("artifact", f"page:{row.query_id}:{ordinal}", ordinal, digest(page))
        point["page_refs"] = [*point["page_refs"], page_ref.to_json()]
        coverage = page["receipt"]["coverage"]
        point["scanned_chunks"] = int(coverage["scanned_chunks"])
        point["snapshot_chunks"] = int(coverage["snapshot_chunks"])
        if page["page_semantics"] == "PROGRESS":
            point["scan_pages_committed"] = int(point["scan_pages_committed"]) + 1
            point["cursor_token"] = cursor
            point["next_wake_at_ms"] = min(row.deadline_ms, self.clock_ms())
            return self._cas(row, point)
        point["result_pages_committed"] = int(point["result_pages_committed"]) + 1
        point["next_result_offset"] = min(512, int(page["receipt"]["page_offset"]) + int(page["receipt"]["returned_count"]))
        point["phase"] = "FETCHING_RESULTS"
        point["cursor_token"] = cursor  # None once the last RESULTS page is in: next step aggregates
        point["next_wake_at_ms"] = min(row.deadline_ms, self.clock_ms())
        return self._cas(row, point)

    def _reload_pages(self, service: SessionSearchService, point: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """After a restart the in-memory pages are gone: re-read them by their frozen refs."""

        pages = service.pages_for_query(str(point["query_id"]))
        refs = point["page_refs"]
        if len(pages) != len(refs) or any(digest(p) != r["content_hash"] for p, r in zip(pages, refs)):
            raise ArpError("RECALL_AGGREGATE_MISMATCH", "frozen pages differ from the checkpointed page refs")
        return list(pages)

    # ---- embedding (C2) -----------------------------------------------------------------

    def _embed_query(self, row: store.ContextRecallRow, point: Mapping[str, Any], request: Mapping[str, Any]) -> Mapping[str, Any]:
        call_key = str(point["query_call_key"])
        if self.embedding is None:
            existing = embedding_call.read_call(self.uow.database.connection, call_key)
            if existing is not None:
                return existing
            raise ArpError("EMBEDDING_UNAVAILABLE", "embedding port missing")
        return embedding_call.perform_call(
            self.uow,
            run_id=row.agent_id,
            call_key=call_key,
            purpose="SESSION_QUERY",
            texts=[request["query_text"]],
            port=self.embedding,
            deployment_ref=self.embedding_deployment_ref or Pin("deployment", "embedding:none", 0, NO_EMBEDDING_FINGERPRINT),
            clock=self.clock,
            clock_ms=self.clock_ms,
            fault=self.fault,
            fault_name="recall.before_embed",
        )

    def _query_vector(self, row: store.ContextRecallRow, point: Mapping[str, Any]) -> tuple[Sequence[float] | None, Pin | None]:
        if point["mode"] != "HYBRID":
            return None, None
        receipt = store.read_original_receipt(self.uow.database.connection, kind=EMBEDDING_CALL_KIND, receipt_key=str(point["query_call_key"]))
        if receipt is None or receipt["status"] != "SUCCEEDED":
            raise ArpError("EMBEDDING_UNAVAILABLE", "hybrid mode without a persisted query vector")
        provider_ref = row.request["embedding_resource_ref"]
        return receipt["output"][0], (Pin.from_json(provider_ref) if provider_ref else None)  # one input → one vector

    # ---- transitions -----------------------------------------------------------------------

    def _cas(self, row: store.ContextRecallRow, point: Mapping[str, Any], result: Mapping[str, Any] | None = None) -> store.ContextRecallRow:
        with self.uow.database.transaction() as connection:
            current = store.read_context_recall(connection, row.recall_key)
            if current is None or current.row_version != row.row_version:
                raise ArpError("RECALL_BINDING_INVALID", "recall row advanced elsewhere")
            return store.cas_context_recall_locked(connection, current, checkpoint=point, result=result)

    def _terminal(self, row: store.ContextRecallRow, point: dict[str, Any], phase: str, code: str) -> store.ContextRecallRow:
        point["phase"] = phase
        point["terminal_error_code"] = code
        point["cursor_token"] = None
        point["next_wake_at_ms"] = min(row.deadline_ms, int(point["last_observed_at_ms"]))
        return self._cas(row, point)

    def _base_result(self, row: store.ContextRecallRow, point: Mapping[str, Any]) -> dict[str, Any]:
        request = row.request
        return {
            "schema_version": 1,
            "recall_key": row.recall_key,
            "request_hash": row.request_hash,
            "original_request_key": row.original_request_key,
            "source_snapshot_hash": request["source_snapshot_hash"],
            "journal_highwater": int(request["journal_highwater"]),
            "exclusions_hash": request["exclusions_hash"],
            "policy_ref": request["policy_ref"],
            "adoption_revision": int(request["adoption_revision"]),
            "index_snapshot_ref": point.get("index_snapshot_ref"),
            "search_query_id": None,
            "coverage": None,
            "query_result_count": None,
            "page_refs": list(point.get("page_refs", [])),
            "page_chain_hash": digest(list(point.get("page_refs", []))),
            "candidate_items": [],
            "candidate_set_hash": digest([]),
            "query_embedding_invocation_ref": point.get("embedding_invocation_ref"),
            "unsettled_call_refs": [],
            "degradations": [],
            "elapsed_ms": max(0, int(point["last_observed_at_ms"]) - int(point["created_at_ms"])),
            "completed_at_ms": int(point["last_observed_at_ms"]),
        }

    def _skipped(self, row: store.ContextRecallRow, point: dict[str, Any], code: str, access: SessionAccess) -> store.ContextRecallRow:
        result = self._base_result(row, point)
        result.update(outcome="SKIPPED", skip_code=code)
        if code in ("EMPTY_QUERY", "RECALL_DISABLED"):
            result.update(status="NOT_REQUESTED", index_snapshot_ref=None, page_refs=[], page_chain_hash=digest([]), query_embedding_invocation_ref=None, search_query_id=None)
        else:
            result.update(status="PARTIAL", search_query_id=row.query_id if point.get("page_refs") else None)
            pages = self._pages.get(row.recall_key) or []
            if pages:
                result["coverage"] = dict(pages[-1]["receipt"]["coverage"])
        point["phase"] = "SKIPPED"
        point["cursor_token"] = None
        point["next_wake_at_ms"] = min(row.deadline_ms, int(point["last_observed_at_ms"]))
        return self._cas(row, point, check("ContextRecallResult", result))

    def _ready(self, row: store.ContextRecallRow, point: dict[str, Any], access: SessionAccess, snapshot: Mapping[str, Any], pages: Sequence[Mapping[str, Any]]) -> store.ContextRecallRow:
        request = row.request
        items = aggregate_context_recall(request, snapshot, pages, control_generation=access.control_generation)
        if len(canonical(items)) > MAX_CANDIDATE_BYTES:
            return self._skipped(row, point, "RECALL_AGGREGATE_LIMIT", access)
        last = pages[-1]["receipt"]
        coverage = dict(last["coverage"])
        result = self._base_result(row, point)
        result.update(
            outcome="READY",
            status=status_for(coverage, int(last["query_result_count"])),
            skip_code=None,
            search_query_id=row.query_id,
            coverage=coverage,
            query_result_count=int(last["query_result_count"]),
            candidate_items=list(items),
            candidate_set_hash=digest(list(items)),
            degradations=list(last["degradations"]),
        )
        point["phase"] = "READY"
        point["next_wake_at_ms"] = min(row.deadline_ms, int(point["last_observed_at_ms"]))
        self._fault_point("recall.before_c6")
        return self._cas(row, point, check("ContextRecallResult", result))

    def _fault_point(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)


def aggregate_context_recall(
    request: Mapping[str, Any], snapshot: Mapping[str, Any], pages: Sequence[Mapping[str, Any]], *, control_generation: int
) -> tuple[Mapping[str, Any], ...]:
    """C5: the only aggregator; every page must belong to the same frozen query."""

    def require(ok: object) -> None:
        if not ok:
            raise ArpError("RECALL_AGGREGATE_MISMATCH")

    require(bool(pages))
    exclusions = sorted(set(request["mandatory_group_ids"]) | set(request["protected_group_ids"]))
    items: list[Mapping[str, Any]] = []
    last_scan = -1
    seen_results = False
    total: int | None = None
    signature = None
    for index, page in enumerate(pages):
        check("ContextSearchPage", dict(page))
        receipt = page["receipt"]
        coverage = receipt["coverage"]
        require(page["cursor_purpose"] == "CONTEXT_RECALL")
        for key in ("session_id", "query_hash", "journal_highwater", "source_snapshot_hash", "policy_ref", "authority_readset_hash"):
            require(receipt[key] == request[key])
        require(receipt["generation"] == control_generation)
        require(receipt["excluded_group_ids"] == exclusions)
        require(receipt["index_snapshot_id"] == snapshot["snapshot_id"])
        require(receipt["index_generation"] == snapshot["index_generation"] and receipt["index_upper_commit"] == snapshot["upper_commit"])
        if index < len(pages) - 1:
            require(page["has_more"])
        if coverage["phase"] == "SCANNING":
            require(not seen_results and coverage["scanned_chunks"] >= last_scan)
            last_scan = coverage["scanned_chunks"]
        else:
            current = (coverage["index_coverage"], coverage["mode"], coverage["snapshot_chunks"], coverage["expected_groups"], coverage["indexed_groups"], coverage["vector_ready_groups"], receipt["query_result_count"])
            if signature is not None:
                require(signature == current)
            signature = current
            seen_results = True
            total = receipt["query_result_count"]
            require(receipt["page_offset"] == len(items))
            items.extend(page["items"])
    require(seen_results and not pages[-1]["has_more"] and len(items) == total)
    require(len({x["chunk_id"] for x in items}) == len(items))
    require([x["rank_ordinal"] for x in items] == list(range(1, len(items) + 1)))
    return tuple(items)


__all__ = (
    "ContextRecallCoordinator",
    "QUERY_PART_LIMITS",
    "QUERY_RECIPE",
    "RecallSources",
    "aggregate_context_recall",
    "build_query",
    "limits_for",
    "recall_key_for",
)
