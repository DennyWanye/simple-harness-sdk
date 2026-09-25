# SPDX-License-Identifier: Apache-2.0
"""Read-only reviewer evidence tools and append-only exposure import (§4, BW06).

The two tools go through the original ToolGateway (identity, permission, schema,
budget, refusal, audit). Each read is authorised by the deployed CURRENT
authority for the review's own use identity, pinned for the review key and
verified byte-exact before any content is returned. A returned value is still
not exposure: only the tool-result message that the model's final actual
Provider request really contained becomes a disclosure batch, and only a
complete UTF-8 read can be cited. Listings and pages never become evidence.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..assurance.certificates import UseIdentity
from ..assurance.codec import AssuranceError, fingerprint, integer, text
from ..assurance.disclosure import DisclosureBatch
from ..assurance.evidence import CatalogueEntry, evidence_label
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.review_input import (
    EVIDENCE_FIND_SCHEMA,
    EVIDENCE_READ_SCHEMA,
    read_tool_disclosures,
)
from ..assurance.reviews import AssuranceReviewBinding
from ..contracts import TERMINAL_MISSION
from ..contracts.models import ContractError
from ..runtime.tool_gateway import ASSURANCE_EVIDENCE_TOOLS, EvidenceToolRefusal, WorkspaceBinding
from ..storage.assurance_blobs import read_pinned_blob
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic

# Bounds of one tool call. They are transport budgets of this reader, not a
# claim that every artifact fits; a larger blob is refused with its size.
# Host real model run 18 (2026-09-23): a MISSION_FINAL reviewer over the whole
# root catalogue made 3+6+4+5 = 18 read-only evidence calls; 16 ended the turn.
MAX_EVIDENCE_TOOL_CALLS = 32
MAX_READ_BYTES = 256 * 1024
DEFAULT_PAGE_CHARS = 4096
# 2026-09-26 真机文档任务: a document rule_check receipt is ~17-19K chars; with an
# 8192 cap no read could ever be complete, so the required check could never be
# cited and every TASK_CONTENT review ended INCONCLUSIVE.
MAX_PAGE_CHARS = 32768
MAX_UNIVERSE_ROWS = 4096
DEFAULT_FIND_LIMIT = 20

ASSURANCE_PROTOCOL = "assurance-exec-v1.1"


def _refuse(code: str, message: str) -> EvidenceToolRefusal:
    return EvidenceToolRefusal(code, message[:500])


class ReviewerEvidenceTools:
    """Orchestrator-side handler installed on the original gateway by the review runtime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.orchestrator = runtime.orchestrator
        self.consumer = runtime.consumer
        self.store = runtime.consumer.store

    # ------------------------------------------------------------- binding
    def install(self, gateway: Any) -> None:
        gateway.assurance_evidence_reader = self.invoke
        gateway.assurance_review_refusal = self.refusal

    def bind(self, agent_id: str, config: Any) -> None:
        """Bind the assured reviewer agent to exactly the read-only evidence tools.

        The legacy verify-workspace authority is never inherited: a review whose
        template lists no tools stays unbound, and any other tool name is refused.
        """
        names = tuple(config.get("agent_config", {}).get("tool_names", ()))
        if not names:
            return
        if set(names) - set(ASSURANCE_EVIDENCE_TOOLS):
            raise ContractError("Assurance review may only bind the read-only evidence tools")
        if config.get("assurance_protocol") != ASSURANCE_PROTOCOL:
            raise ContractError("Assurance evidence tools require the assured review protocol")
        review_key = text(config["review_key"])
        row = self.store.connection.execute(
            "SELECT mission_id FROM assurance_review_bindings WHERE review_key=?", (review_key,)
        ).fetchone()
        if row is None:
            raise ContractError("Assurance review binding is not persisted")
        gateway = self.orchestrator.assembled.gateway
        self.install(gateway)
        gateway.bind(
            agent_id,
            WorkspaceBinding(
                # Not an Attempt: root/method/proposal reviews own no workspace, and
                # a TASK_CONTENT review keeps the Attempt id for audit attribution only.
                str(config.get("attempt_id") or ""),
                "verify",
                False,
                names,
                max_tool_calls=MAX_EVIDENCE_TOOL_CALLS,
                mission_id=row["mission_id"],
                review_key=review_key,
            ),
        )

    # ------------------------------------------------------------ refusal
    def _live_intent(self, run_id: str, review_key: str) -> Any:
        rows = self.store.connection.execute(
            "SELECT intent_id FROM dispatch_intents WHERE agent_id=?", (run_id,)
        ).fetchall()
        if len(rows) != 1:
            return None
        intent = self.store.get_intent(rows[0][0])
        if (
            intent is None
            or intent.config.get("assurance_protocol") != ASSURANCE_PROTOCOL
            or intent.config.get("review_key") != review_key
            or intent.state not in {"AGENT_CREATED", "SUBMITTED"}
        ):
            return None
        return intent

    def refusal(self, run_id: str, binding: WorkspaceBinding) -> str | None:
        """Re-read the live invocation and subject before each physical read."""
        from ..orchestrator.assurance_review_import import review_subject_stopped

        if binding.review_key is None:
            return "assurance_review_unbound"
        with self.store.read_view():
            intent = self._live_intent(run_id, binding.review_key)
            if intent is None:
                return "assurance_review_intent_unavailable"
            mission = self.store.get_mission(intent.mission_id)
            if mission is None or mission.status in TERMINAL_MISSION:
                return "assurance_review_stopped"
            review = self._review_binding(binding.review_key)
            if review is None or review_subject_stopped(self.store, review):
                return "assurance_review_stopped"
        return None

    # ------------------------------------------------------------- reading
    def _review_binding(self, review_key: str) -> AssuranceReviewBinding | None:
        row = self.store.connection.execute(
            "SELECT binding_json, binding_hash FROM assurance_review_bindings WHERE review_key=?",
            (review_key,),
        ).fetchone()
        if row is None:
            return None
        binding = AssuranceReviewBinding(row["binding_json"])
        if binding.content_hash != row["binding_hash"]:
            raise AssuranceError("REVIEW_BINDING_SOURCE_MISMATCH")
        return binding

    def _identity(self, binding: AssuranceReviewBinding) -> UseIdentity:
        body = binding.to_json()
        from ..orchestrator.assurance_review_import import review_scope_id

        try:
            scope_id = review_scope_id(body)
        except AssuranceError as error:
            raise _refuse(error.code, "this review has no completion Scope yet") from error
        root = self.orchestrator.commit._assurance_root_gate.require_execution()
        return UseIdentity(
            body["mission_id"],
            "REVIEW",
            body["review_key"],
            scope_id,
            self.consumer.principal_id,
            "DISCLOSE",
            root.root_incarnation_id,
        )

    def _universe(self, reader: AssuranceReader, binding: AssuranceReviewBinding) -> list[dict]:
        """Deterministic candidate list: frozen catalogue, then registered blobs.

        It is a visible listing for the reviewer, not a COMPLETE evidence claim.
        """
        body = binding.to_json()
        review_key = body["review_key"]
        rows: dict[str, dict] = {}
        for item in body["evidence_catalogue"]:
            ref = AssuranceRef.from_json(item["ref"])
            rows[item["label"]] = {"label": item["label"], "ref": ref, "initial": True}
        connection = reader.store.connection
        sources = connection.execute(
            "SELECT path, revision, version_hash FROM sources WHERE mission_id=? AND tenant_id=? "
            "AND superseded_by IS NULL AND revoked=0 ORDER BY path, version_hash LIMIT ?",
            (reader.mission_id, reader.tenant_id, MAX_UNIVERSE_ROWS + 1),
        ).fetchall()
        artifacts = connection.execute(
            "SELECT artifact_id, version, content_hash, path FROM artifacts WHERE mission_id=? "
            "ORDER BY artifact_id, version LIMIT ?",
            (reader.mission_id, MAX_UNIVERSE_ROWS + 1),
        ).fetchall()
        if len(sources) > MAX_UNIVERSE_ROWS or len(artifacts) > MAX_UNIVERSE_ROWS:
            raise _refuse("EVIDENCE_LISTING_INCOMPLETE", "too many registered blobs to list")
        for row in sources:
            ref = AssuranceRef("source", Pin(row["path"], row["revision"], row["version_hash"]))
            label = evidence_label(review_key, ref)
            rows.setdefault(label, {"label": label, "ref": ref, "initial": False})
        for row in artifacts:
            ref = AssuranceRef(
                "artifact", Pin(row["artifact_id"], row["version"], row["content_hash"])
            )
            label = evidence_label(review_key, ref)
            rows.setdefault(label, {"label": label, "ref": ref, "initial": False})
        return [rows[label] for label in sorted(rows)]

    def _context(self, run_id: str, mission_id: str):  # type: ignore[no-untyped-def]
        from ..orchestrator.assurance_check_use import _permission

        mission = self.store.get_mission(mission_id)
        if mission is None or mission.tenant_id != self.consumer.tenant_id:
            raise _refuse("REF_SCOPE_MISMATCH", "unknown Mission for this review")
        with self.store.read_view():
            rows = self.store.connection.execute(
                "SELECT intent_id FROM dispatch_intents WHERE agent_id=?", (run_id,)
            ).fetchall()
            intent = None if len(rows) != 1 else self.store.get_intent(rows[0][0])
            review_key = None if intent is None else intent.config.get("review_key")
            if (
                intent is None
                or intent.mission_id != mission_id
                or intent.config.get("assurance_protocol") != ASSURANCE_PROTOCOL
                or not isinstance(review_key, str)
            ):
                raise _refuse("assurance_review_intent_unavailable", "no live review for agent")
            binding = self._review_binding(review_key)
            if binding is None or AssuranceStore(self.store).lane(mission_id) != "ASSURANCE_1_1":
                raise _refuse("REVIEW_BINDING_SOURCE_MISMATCH", "review binding unavailable")
        reader = AssuranceReader(self.store, tenant_id=mission.tenant_id, mission_id=mission_id)
        identity = self._identity(binding)

        def authorize(ref: AssuranceRef):  # type: ignore[no-untyped-def]
            return _permission(
                self.consumer.authority, identity, ref, int(self.store.now * 1000)
            ).access

        return reader, binding, identity, authorize

    def invoke(self, run_id: str, mission_id: str, tool: str, arguments: Any) -> dict:
        """Gateway callback: current permission → pinned exact read → labelled content."""
        try:
            reader, binding, identity, authorize = self._context(run_id, mission_id)
            if tool == "assurance_find_evidence":
                return self._find(reader, binding, identity, dict(arguments))
            if tool == "assurance_read_evidence":
                return self._read(reader, binding, identity, authorize, dict(arguments))
        except EvidenceToolRefusal:
            raise
        except AssuranceError as error:
            raise _refuse(error.code, "evidence read refused: " + error.code) from error
        raise _refuse("unknown_tool", tool)

    def _find(self, reader, binding, identity, arguments) -> dict:  # type: ignore[no-untyped-def]
        from ..orchestrator.assurance_check_use import _permission

        query = arguments.get("query")
        offset = integer(arguments.get("offset", 0), minimum=0)
        limit = integer(arguments.get("limit", DEFAULT_FIND_LIMIT), minimum=1, maximum=50)
        universe = self._universe(reader, binding)
        if isinstance(query, str) and query:
            needle = query.casefold()
            universe = [
                row
                for row in universe
                if needle in row["ref"].kind.casefold() or needle in row["ref"].pin.id.casefold()
            ]
        page = universe[offset : offset + limit]
        entries, hidden = [], 0
        now_ms = int(self.store.now * 1000)
        for row in page:
            try:
                _permission(self.consumer.authority, identity, row["ref"], now_ms)
            except AssuranceError:
                hidden += 1
                continue
            ref = row["ref"]
            entries.append(
                {
                    "label": row["label"],
                    "ref": ref.to_json(),
                    "kind": ref.kind,
                    "id": ref.pin.id,
                    "revision": ref.pin.revision,
                    "in_initial_catalogue": row["initial"],
                }
            )
        next_offset = offset + limit if offset + limit < len(universe) else None
        return {
            "schema": EVIDENCE_FIND_SCHEMA,
            "review_key": binding.to_json()["review_key"],
            "entries": entries,
            "offset": offset,
            "next_offset": next_offset,
            "total": len(universe),
            "denied_hidden": hidden,
            "disclosure": "LISTING_NOT_EXPOSURE",
        }

    def _read(self, reader, binding, identity, authorize, arguments) -> dict:  # type: ignore[no-untyped-def]
        from ..orchestrator.assurance_check_use import _permission
        from ..orchestrator.assurance_review_pins import ensure_review_blob_pins

        label = text(arguments.get("label", ""))
        offset = integer(arguments.get("offset", 0), minimum=0)
        max_chars = integer(
            arguments.get("max_chars", DEFAULT_PAGE_CHARS), minimum=1, maximum=MAX_PAGE_CHARS
        )
        body = binding.to_json()
        review_key = body["review_key"]
        universe = self._universe(reader, binding)
        match = next((row for row in universe if row["label"] == label), None)
        if match is None:
            raise _refuse("EVIDENCE_LABEL_UNKNOWN", "no such evidence label for this review")
        ref = match["ref"]
        # Current permission first; the read below re-checks it at final use.
        _permission(self.consumer.authority, identity, ref, int(self.store.now * 1000))
        if ref.kind in {"source", "artifact"}:
            pins = ensure_review_blob_pins(
                self.orchestrator.commit,
                tenant_id=reader.tenant_id,
                binding=binding,
                refs=(ref,),
            )
            data = read_pinned_blob(
                reader,
                ref,
                pin_id=pins[ref],
                review_key=review_key,
                cas=self.consumer.cas,
                maximum_bytes=MAX_READ_BYTES,
                authorize=authorize,
                now_ms=lambda: int(self.store.now * 1000),
            ).data
        else:
            data = reader.read_exact_metadata(ref).body_json.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.pin.content_hash:
            raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
        try:
            content = data.decode("utf-8", errors="strict")
        except UnicodeError:
            raise _refuse(
                "REVIEW_MATERIAL_CODEC_UNSUPPORTED",
                f"evidence {label} is not UTF-8 text ({len(data)} bytes, sha256 {digest}); "
                "binary material cannot be disclosed through this tool",
            ) from None
        if offset > len(content):
            raise _refuse("EVIDENCE_OFFSET_OUT_OF_RANGE", "offset is past the end of the material")
        page = content[offset : offset + max_chars]
        complete = offset == 0 and len(page) == len(content)
        next_offset = offset + len(page) if offset + len(page) < len(content) else None
        result = {
            "schema": EVIDENCE_READ_SCHEMA,
            "review_key": review_key,
            "label": label,
            "ref": ref.to_json(),
            "kind": ref.kind,
            "encoding": "utf8",
            "content": page,
            "offset": offset,
            "next_offset": next_offset,
            "size_bytes": len(data),
            "total_chars": len(content),
            "sha256": digest,
            "complete": complete,
            "disclosure": "COMPLETE_IN_MODEL_INPUT_ONLY" if complete else "PARTIAL_NOT_CITABLE",
        }
        if not complete and len(content) <= MAX_PAGE_CHARS:
            result["complete_read_hint"] = (
                f"re-read with offset=0 and max_chars={len(content)} for a complete, citable read"
            )
        return result


# ------------------------------------------------------------ exposure import
def record_disclosure_batch(
    commit: Any,
    reader: AssuranceReader,
    *,
    review_key: str,
    turn_ref: AssuranceRef,
    agent_id: str,
    manifest: dict,
    entries: tuple[CatalogueEntry, ...],
    message_ids: tuple[str, ...],
) -> None:
    """Append one batch bound to this exact turn/input; same-body replay adds nothing."""
    # DisclosureBatch keeps visible_message_ids as a sorted set; the delivery
    # event must carry the same canonical order or the store's event/batch
    # binding refuses it (Host real model run 14, 2026-09-23: two tool-result
    # messages in request order ≠ lexical order → DISCLOSURE_INPUT_BINDING).
    message_ids = tuple(sorted(message_ids))
    with atomic(commit.store):
        side = AssuranceStore(commit.store)
        chain = side.disclosure_chain(reader.mission_id, review_key)
        same = [
            b
            for b in chain
            if b.turn_receipt_ref == turn_ref and set(b.visible_message_ids) == set(message_ids)
        ]
        if same:
            if (
                len(same) != 1
                or same[0].entries != tuple(sorted(entries, key=lambda e: e.label))
                or same[0].provider_input_hash != manifest["provider_input_hash"]
                or same[0].reviewer_agent_id != agent_id
            ):
                raise AssuranceError("DISCLOSURE_INPUT_BINDING")
            return
        no = len(chain)
        previous = None if not chain else chain[-1].content_hash
        ordered = tuple(sorted(entries, key=lambda e: e.label))
        payload = {
            "review_key": review_key,
            "batch_no": no,
            "previous_batch_hash": previous,
            "delta_hash": fingerprint([entry.to_json() for entry in ordered]),
            "reviewer_agent_id": agent_id,
            "turn_receipt_ref": turn_ref.to_json(),
            "provider_input_hash": manifest["provider_input_hash"],
            "visible_message_ids": list(message_ids),
        }
        event = commit._emit(
            "AssuranceEvidenceDisclosed",
            reader.mission_id,
            key=review_key + ":" + str(no),
            payload=payload,
        )
        ref = AssuranceRef("disclosure_receipt", Pin(event.id, 0, fingerprint(event.to_json())))
        side.record_disclosure(
            DisclosureBatch(
                reader.mission_id,
                review_key,
                no,
                previous,
                ordered,
                agent_id,
                turn_ref,
                manifest["provider_input_hash"],
                message_ids,
                ref,
            )
        )


def import_reviewer_disclosure(
    commit: Any,
    reader: AssuranceReader,
    binding: AssuranceReviewBinding,
    turn_ref: AssuranceRef,
    agent_id: str,
    manifest: dict,
) -> tuple[CatalogueEntry, ...]:
    """Tool-result messages that the final actual Provider request really contained.

    A value the tool returned but that never entered the model input is not in
    the manifest and therefore discloses nothing. Duplicate complete reads of
    one exact ref share its label; conflicting labels are refused by the codec.
    """
    review_key = binding.to_json()["review_key"]
    labelled: dict[str, CatalogueEntry] = {}
    message_ids = []
    for row in manifest["messages"]:
        if row["kind"] != "tool_result":
            continue
        found = read_tool_disclosures(row["message"], review_key)
        if not found:
            continue
        for entry in found:
            if labelled.setdefault(entry.label, entry) != entry:
                raise AssuranceError("LABEL_COLLISION")
        message_ids.append(row["message_id"])
    if not labelled:
        return ()
    entries = tuple(labelled[label] for label in sorted(labelled))
    record_disclosure_batch(
        commit,
        reader,
        review_key=review_key,
        turn_ref=turn_ref,
        agent_id=agent_id,
        manifest=manifest,
        entries=entries,
        message_ids=tuple(message_ids),
    )
    return entries


__all__ = (
    "MAX_EVIDENCE_TOOL_CALLS",
    "ReviewerEvidenceTools",
    "import_reviewer_disclosure",
    "record_disclosure_batch",
)
