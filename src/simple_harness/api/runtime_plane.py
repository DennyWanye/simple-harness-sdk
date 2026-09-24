# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``RuntimePlaneService``: the 23 Host verbs of ``contracts/host-verbs.json`` as typed calls
(HOST-DTOS §1–§7, INTERFACES).

The Host's authenticated dispatcher decodes nothing itself: it hands the ``HostRequest``
envelope plus the fixed ``TrustedCaller`` to ``handle`` and returns the ``HostResponse``
it gets back.  Every write goes through the owning ARP service (creation / catalogue /
skills / lifecycle / session lifecycle / policy store); this module only checks the
envelope (verb table, one payload, revision pairs, cursor/limit echo, subject binding),
records one ``host_command`` receipt per successful write (same command id → the
original receipt and the original result, never today's), and maps failures to the
public ``Error`` DTO by catalogue code.  Nothing in a payload can name a caller, a
tenant or a path.
"""

from __future__ import annotations

import inspect
from typing import Any, Mapping, Sequence

from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp import store
from simple_harness.agents.arp.codec import check, host_verbs
from simple_harness.agents.arp.errors import ArpError, entry as error_entry
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import TrustedCaller
from simple_harness.agents.arp.strict import canonical, digest, parse_strict, plain

INLINE_PAYLOAD_MAX = 256 * 1024
COMMAND_RECEIPT_KIND = "host_command"
MANIFEST_FIELDS = ("HEADER", "SECTIONS", "RECENT_GROUPS", "RECALL", "AUTHORITIES")
_RETRY_PUBLIC = {"NEVER": "NEVER", "BOUNDED_SAME_ID": "BOUNDED_SAME_ID", "SAME_DESTROY_BACKOFF": "BOUNDED_SAME_ID"}
SESSION_VERBS = frozenset({
    "agent_context_summary", "agent_context_history", "agent_context_settings_get", "agent_context_settings_update",
    "agent_context_manifest", "agent_session_history_search", "agent_session_history_read", "agent_session_destroy",
    "agent_session_rebuild", "agent_session_destroy_resume",
})


def error_json(error: ArpError) -> dict[str, Any]:
    """Public ``Error`` DTO: catalogue stage / public message, retry folded to the DTO enum."""

    row = error_entry(error.code)
    return {
        "schema_version": 1,
        "code": error.code,
        "stage": str(row["stage"]),
        "retry": _RETRY_PUBLIC.get(str(row["retry"]), "AFTER_RESOURCE_OR_AUTH"),
        "field_path": error.field_path,
        "message": str(row["public_message"]),
        "source_refs": [],
    }


class RuntimePlaneService:
    def __init__(self, runtime: Any) -> None:
        arp = getattr(runtime, "arp", None)
        if arp is None or arp.sessions is None or arp.catalogue is None or arp.skills is None or arp.lifecycle is None or arp.retriever is None:
            raise ArpError("STATE_COMBINATION_INVALID", "runtime plane service needs a fully assembled ARP runtime")
        self.runtime = runtime
        self.arp = arp

    # ---- shared helpers ----------------------------------------------------------------------

    @property
    def connection(self):  # type: ignore[no-untyped-def]
        return self.runtime.uow.database.connection

    @property
    def catalogue(self) -> cat.CatalogueService:
        return self.arp.catalogue  # type: ignore[return-value]

    def _now_ms(self) -> int:
        return int(self.arp.ports.clock_ms())

    @staticmethod
    def _caller(caller: TrustedCaller | None) -> TrustedCaller:
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "host verbs need the Host's fixed authenticated caller")
        return caller

    def _session(self, session_id: str, *, subject_id: str | None = None) -> store.SessionRow:
        session = store.read_session(self.connection, session_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "unknown session")
        if subject_id is not None and session.agent_id != subject_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "the session belongs to another subject agent")
        return session

    def _adoption(self, session: store.SessionRow) -> tuple[store.PolicyAdoptionRow, store.PolicyObjectRow]:
        adoption = store.latest_adoption(self.connection, session.session_id)
        if adoption is None:
            raise ArpError("POLICY_CONFLICT", "session has no adopted context policy")
        policy = store.read_policy_object(self.connection, adoption.policy_ref.id, adoption.policy_ref.revision)
        if policy is None or policy.content_hash != adoption.policy_ref.content_hash:
            raise ArpError("POLICY_CONFLICT", "adopted policy object is missing or differs")
        return adoption, policy

    def _context_eventseq(self, context_id: str | None, session_id: str) -> int:
        """``RuntimeContextPrepared`` original eventseq of one context (or the session's latest)."""

        if context_id is None:
            row = self.connection.execute(
                "SELECT MAX(original_eventseq) FROM arp_event_bindings WHERE event_type='RuntimeContextPrepared'"
                " AND json_extract(body_json,'$.session_ref.id')=?",
                (session_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT MAX(original_eventseq) FROM arp_event_bindings WHERE event_type='RuntimeContextPrepared'"
                " AND json_extract(body_json,'$.context_ref.id')=?",
                (context_id,),
            ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    def _context_row(self, session: store.SessionRow, context_id: str | None) -> store.ContextRequestRow | None:
        if context_id is None:
            return store.latest_context(self.connection, session.session_id)
        row = store.read_context(self.connection, context_id)
        if row is None or row.session_id != session.session_id:
            raise ArpError("REF_OUTSIDE_SCOPE", "context does not belong to this session")
        return row

    def _skill_item(self, revision: cat.RevisionRow) -> dict[str, Any]:
        activation = cat.read_activation(self.connection, self.catalogue.namespace_id, "SKILL", revision.entry_id, revision.revision)
        if activation is None:
            raise ArpError("CATALOGUE_STALE", "skill activation row missing")
        return check("SkillCatalogueItem", self.catalogue.summary(revision, activation, access_view="MANAGEMENT", now_ms=self._now_ms()))

    def _skill(self, skill_ref: Mapping[str, Any]) -> cat.RevisionRow:
        revision = cat.resolve_pin(self.connection, self.catalogue.namespace_id, Pin.from_json(skill_ref))
        if revision.entry_kind != "SKILL":
            raise ArpError("CATALOGUE_KIND_MISMATCH", "not a skill")
        return revision

    def _artifact_bytes(self, ref: Pin) -> bytes:
        reader = self.arp.ports.artifacts
        if reader is None:
            raise ArpError("SOURCE_UNAVAILABLE", "no authenticated artifact reader is assembled")
        ref.require_kind("artifact")
        data = reader(ref)
        if not isinstance(data, (bytes, bytearray)):
            raise ArpError("SOURCE_UNAVAILABLE", "artifact reader returned no bytes")
        if digest_bytes(bytes(data)) != ref.content_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", "artifact bytes differ from the pinned hash")
        return bytes(data)

    # ---- context: summary / history / manifest ---------------------------------------------------

    def _retrieval_summary(self, manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
        recall_key = manifest["retrieval_receipt_ref"]["id"]
        row = store.read_context_recall(self.connection, recall_key)
        if row is None or row.result is None:
            return None
        result = row.result
        ready = result["outcome"] == "READY"
        summary = {
            "outcome": result["outcome"],
            "status": result["status"],
            "skip_code": result["skip_code"],
            "coverage": result["coverage"],
            "query_result_count": result["query_result_count"] if ready else None,
            "candidate_count": len(result["candidate_items"]) if ready else 0,
            "selected_count": len(manifest["recalled_chunk_ids"]) if ready else 0,
            "aggregate_ref": dict(manifest["retrieval_receipt_ref"]),
        }
        return check("RetrievalSummary", summary)

    def context_summary(self, payload: Mapping[str, Any], *, subject_id: str | None = None) -> dict[str, Any]:
        value = check("ContextReadCommand", plain(payload))
        session = self._session(str(value["session_id"]), subject_id=subject_id)
        adoption, policy = self._adoption(session)
        latest = store.latest_context(self.connection, session.session_id)
        row = self._context_row(session, value["context_id"])
        body: dict[str, Any] = {
            "schema_version": 2,
            "session_id": session.session_id,
            "context_id": None,
            "historical": False,
            "manifest_ref": None,
            "input_charge": None,
            "input_budget": None,
            "output_reserve": None,
            "recent_complete_turns": None,
            "recall_count": None,
            "retrieval_status": None,
            "session_state": session.state,
            "current_access": "ALLOWED",
            "degradations": [],
            "adoption_revision": adoption.adoption_revision,
            "current_effective_policy_ref": adoption.policy_ref.to_json(),
            "manifest_policy_ref": None,
            "configured_context_tokens": int(policy.body["max_context_tokens"]),
            "last_count_mode": None,
            "prior_output_reserve_tokens": None,
            "next_request_policy_ref": adoption.policy_ref.to_json(),
            "session_generation": session.generation,
            "retrieval_summary": None,
        }
        if row is not None:
            manifest = row.manifest
            summary = self._retrieval_summary(manifest)
            body.update(
                context_id=row.context_id,
                historical=latest is None or latest.context_id != row.context_id,
                manifest_ref=row.pin.to_json(),
                input_charge=row.input_charge,
                input_budget=row.input_budget,
                output_reserve=row.output_reserve,
                recent_complete_turns=int(manifest["recent_complete_turn_count"]),
                recall_count=summary["selected_count"] if summary is not None else 0,
                retrieval_status=summary["status"] if summary is not None else "NOT_REQUESTED",
                manifest_policy_ref=dict(manifest["effective_context_policy_ref"]),
                last_count_mode=manifest["counter_mode"],
                prior_output_reserve_tokens=int(manifest["prior_output_reserve_tokens"]),
                retrieval_summary=summary,
            )
            recall_row = store.read_context_recall(self.connection, manifest["retrieval_receipt_ref"]["id"])
            if recall_row is not None and recall_row.result is not None:
                body["degradations"] = [str(d) for d in recall_row.result.get("degradations", [])][:32]
        return check("ContextSummaryView", body)

    @staticmethod
    def _manifest_header(row: store.ContextRequestRow) -> dict[str, Any]:
        manifest = row.manifest
        return {
            "context_id": row.context_id, "manifest_hash": row.manifest_hash, "turn_id": row.turn_id,
            "provider_request_ordinal": row.provider_request_ordinal, "session_generation": row.session_generation,
            "journal_highwater": row.journal_highwater, "input_token_charge": manifest["input_token_charge"],
            "effective_input_budget": manifest["effective_input_budget"], "reserved_output_tokens": manifest["reserved_output_tokens"],
            "adoption_revision": manifest["adoption_revision"], "effective_context_policy_ref": manifest["effective_context_policy_ref"],
            "recent_complete_turn_count": manifest["recent_complete_turn_count"], "recalled_count": len(manifest["recalled_chunk_ids"]),
            "created_at_ms": row.created_at_ms,
        }

    @staticmethod
    def _cursor(prefix: str, offset: int, field: str | None = None) -> str:
        return f"{prefix}:{field}:{offset}" if field is not None else f"{prefix}:{offset}"

    def context_manifest(self, payload: Mapping[str, Any], *, subject_id: str | None = None) -> dict[str, Any]:
        """One frozen manifest paged by field (HEADER → SECTIONS → RECENT_GROUPS → RECALL → AUTHORITIES)."""

        value = check("ContextReadCommand", plain(payload))
        session = self._session(str(value["session_id"]), subject_id=subject_id)
        row = self._context_row(session, value["context_id"])
        if row is None:
            raise ArpError("SOURCE_UNAVAILABLE", "the session has no frozen context yet")
        manifest = row.manifest
        fields: dict[str, list[Any]] = {
            "HEADER": [self._manifest_header(row)],
            "SECTIONS": list(manifest["sections"]),
            "RECENT_GROUPS": list(manifest["recent_group_ids"]),
            "RECALL": list(manifest["recalled_chunk_ids"]),
            "AUTHORITIES": list(manifest["authority_refs"]),
        }
        field, offset = "HEADER", 0
        if value["cursor"] is not None:
            parts = str(value["cursor"]).split(":")
            if len(parts) != 3 or parts[0] != row.manifest_hash[:16] or parts[1] not in MANIFEST_FIELDS:
                raise ArpError("CURSOR_UNKNOWN", "cursor belongs to another manifest")
            try:
                offset = int(parts[2])
            except ValueError as error:
                raise ArpError("CURSOR_UNKNOWN") from error
            field = parts[1]
            if offset > len(fields[field]):
                raise ArpError("CURSOR_UNKNOWN", "cursor offset beyond the field")
        limit = int(value["limit"])
        items = fields[field][offset : offset + limit]
        end = offset + len(items)
        index = MANIFEST_FIELDS.index(field)
        if end < len(fields[field]):
            next_cursor: str | None = self._cursor(row.manifest_hash[:16], end, field)
        elif index + 1 < len(MANIFEST_FIELDS):
            next_cursor = self._cursor(row.manifest_hash[:16], 0, MANIFEST_FIELDS[index + 1])
        else:
            next_cursor = None
        page = {
            "context_ref": row.pin.to_json(), "manifest_hash": row.manifest_hash, "field": field, "offset": offset,
            "items": items, "has_more": next_cursor is not None, "next_cursor": next_cursor,
        }
        return check("ContextManifestPage", page)

    def context_history(self, payload: Mapping[str, Any], *, subject_id: str | None = None) -> dict[str, Any]:
        """Headers of the session's frozen contexts, oldest first, bound to the eventseq upper bound."""

        value = check("ContextReadCommand", plain(payload))
        session = self._session(str(value["session_id"]), subject_id=subject_id)
        upper = self._context_eventseq(None, session.session_id)
        rows = self.connection.execute(
            "SELECT context_id FROM arp_context_requests WHERE session_id=? ORDER BY created_at_ms, provider_request_ordinal, context_id",
            (session.session_id,),
        ).fetchall()
        ids = [str(r[0]) for r in rows]
        offset = 0
        if value["cursor"] is not None:
            parts = str(value["cursor"]).split(":")
            try:
                if len(parts) != 2 or int(parts[0]) != upper:
                    raise ArpError("CURSOR_STALE", "the context history advanced since this cursor")
                offset = int(parts[1])
            except ValueError as error:
                raise ArpError("CURSOR_UNKNOWN") from error
            if offset > len(ids):
                raise ArpError("CURSOR_UNKNOWN", "cursor offset beyond the history")
        latest = self._context_row(session, value["context_id"])
        if latest is None:
            raise ArpError("SOURCE_UNAVAILABLE", "the session has no frozen context yet")
        chosen = ids[offset : offset + int(value["limit"])]
        items = []
        for context_id in chosen:
            row = store.read_context(self.connection, context_id)
            assert row is not None
            items.append(self._manifest_header(row))
        end = offset + len(items)
        page = {
            "context_ref": latest.pin.to_json(), "manifest_hash": latest.manifest_hash, "field": "HEADER", "offset": offset,
            "items": items, "has_more": end < len(ids), "next_cursor": f"{upper}:{end}" if end < len(ids) else None,
        }
        return check("ContextManifestPage", page)

    # ---- context: settings / policy ---------------------------------------------------------------

    def settings_get(self, payload: Mapping[str, Any], *, subject_id: str | None = None) -> dict[str, Any]:
        value = check("SessionReadCommand", plain(payload))
        session = self._session(str(value["session_id"]), subject_id=subject_id)
        return self._settings_view(session)

    def _settings_view(self, session: store.SessionRow, *, adoption: store.PolicyAdoptionRow | None = None) -> dict[str, Any]:
        if adoption is None:
            adoption, policy = self._adoption(session)
        else:
            policy = store.read_policy_object(self.connection, adoption.policy_ref.id, adoption.policy_ref.revision)
            if policy is None or policy.content_hash != adoption.policy_ref.content_hash:
                raise ArpError("POLICY_CONFLICT", "adopted policy object is missing or differs")
        latest = store.latest_context(self.connection, session.session_id)
        body = {
            "schema_version": 1,
            "agent_id": session.agent_id,
            "session_id": session.session_id,
            "session_generation": session.generation,
            "adoption_revision": adoption.adoption_revision,
            "effective_policy_ref": adoption.policy_ref.to_json(),
            "configured_policy": dict(policy.body),
            "activation_ref": self.arp.ports.profile.refs.activation_receipt_ref.to_json(),
            "policy_approval_ref": policy.approval_ref.to_json(),
            "next_request_only": True,
            "last_context_ref": None if latest is None else latest.pin.to_json(),
            "view_revision": adoption.adoption_revision,
        }
        return check("ContextSettingsView", body)

    def policy_get(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = check("PolicyReadCommand", plain(payload))
        ref = Pin.from_json(value["policy_ref"])
        policy = store.read_policy_object(self.connection, ref.id, ref.revision)
        if policy is None or policy.content_hash != ref.content_hash:
            raise ArpError("REF_IDENTITY_MISMATCH", "no policy object with this pin")
        return check("Policy", dict(policy.body))

    def policy_submit(self, payload: Mapping[str, Any], *, caller: TrustedCaller, command_id: str) -> tuple[dict[str, Any], store.PolicyObjectRow]:
        """Store one approved policy body as the next revision of its policy id (not adopted)."""

        caller = self._caller(caller)
        value = check("ContextPolicySubmit", plain(payload))
        from simple_harness.agents.arp.profile import check_policy

        body = check_policy(value["policy"])
        policy_id = str(body["policy_id"])
        connection = self.connection
        current_revision = store.latest_policy_revision(connection, policy_id)
        if current_revision:
            latest = store.read_policy_object(connection, policy_id, current_revision)
            if latest is not None and latest.approval_ref.id == f"{policy_id}:submit:{command_id}":
                if latest.content_hash != digest(body):
                    raise ArpError("EXPECTED_REVISION_MISMATCH", "command id reused with another policy body")
                return {"policy_ref": latest.pin.to_json(), "approval_ref": latest.approval_ref.to_json()}, latest
        expected = value["expected_policy_ref"]
        if expected is not None:
            pin = Pin.from_json(expected)
            current = store.read_policy_object(connection, policy_id, current_revision) if current_revision else None
            if pin.id != policy_id or current is None or current.pin != pin:
                raise ArpError("POLICY_CONFLICT", "expected policy ref is not the current revision")
        elif current_revision:
            raise ArpError("POLICY_CONFLICT", "policy id already has revisions; name the expected one")
        revision = current_revision + 1
        approval = Pin("authority", f"{policy_id}:submit:{command_id}", revision, digest({"caller": caller.to_json(), "policy": body}))
        with self.runtime.uow.database.transaction() as txn:
            row = store.put_policy_object_locked(txn, body=body, revision=revision, approval_ref=approval, source_receipt_ref=caller.command_receipt_ref)
        return {"policy_ref": row.pin.to_json(), "approval_ref": row.approval_ref.to_json()}, row

    def settings_update(self, payload: Mapping[str, Any], *, caller: TrustedCaller, command_id: str, subject_id: str | None = None) -> dict[str, Any]:
        """Adopt an already approved candidate policy for the session's *next* requests."""

        caller = self._caller(caller)
        value = check("ContextSettingsCommand", plain(payload))
        session = self._session(str(value["session_id"]), subject_id=subject_id)
        connection = self.connection
        command_hash = digest({"kind": "settings_update", "command": value, "caller": caller.to_json()})
        replay = store.read_adoption_by_command(connection, command_id)
        if replay is not None:
            # Owner-side evidence of this exact command: the same body replays the adoption
            # it produced (never a later one); another body is a conflict.
            if replay.command_hash != command_hash or replay.session_id != session.session_id:
                raise ArpError("EXPECTED_REVISION_MISMATCH", "command id reused with another adoption")
            return self._settings_view(session, adoption=replay)
        if session.state != "ACTIVE":
            raise ArpError("SESSION_DRAINING" if session.state == "DRAINING" else "SESSION_NOT_ACTIVE", f"session is {session.state}")
        adoption, _ = self._adoption(session)
        expected_ref = Pin.from_json(value["expected_effective_policy_ref"])
        if adoption.adoption_revision != int(value["expected_adoption_revision"]) or adoption.policy_ref != expected_ref:
            raise ArpError("POLICY_CONFLICT", "another adoption happened since the settings were read")
        candidate_ref = Pin.from_json(value["candidate_policy_ref"])
        candidate = store.read_policy_object(connection, candidate_ref.id, candidate_ref.revision)
        if candidate is None or candidate.content_hash != candidate_ref.content_hash:
            raise ArpError("POLICY_CONFLICT", "candidate policy is not an approved policy object")
        now_ms = self._now_ms()
        with self.runtime.uow.database.transaction() as txn:
            adopted = store.append_policy_adoption_locked(
                txn, session_id=session.session_id, policy=candidate, command_id=command_id, command_hash=command_hash,
                authority_receipt_ref=self.arp.ports.profile.refs.activation_receipt_ref, source_receipt_ref=caller.command_receipt_ref, now_ms=now_ms,
            )
            store.append_runtime_event_locked(
                txn,
                run_id=session.agent_id,
                event_type="AgentContextPolicyAdopted",
                body={
                    "session_ref": session.pin.to_json(),
                    "adoption_revision": adopted.adoption_revision,
                    "policy_ref": candidate.pin.to_json(),
                    "command_receipt_ref": caller.command_receipt_ref.to_json(),
                },
                source_receipt_ref=caller.command_receipt_ref,
                dedupe_key=f"{session.session_id}:adoption:{adopted.adoption_revision}",
                now=self.runtime.ports.clock(),
            )
        return self._settings_view(session, adoption=adopted)

    # ---- command receipts -----------------------------------------------------------------------------

    def _command_receipt(self, command_id: str, *, subject_id: str | None = None) -> tuple[dict[str, Any], Any] | None:
        """The stored receipt + original result; a receipt of another subject reads as absent
        (HOST-DTOS §6: replay still needs the current read right, no probing by command id)."""

        stored = store.read_host_command(self.connection, command_id)
        if stored is None or (subject_id is not None and stored["subject_id"] != subject_id):
            return None
        body = stored["body"]
        receipt = Pin("receipt", f"{COMMAND_RECEIPT_KIND}:{command_id}", 0, stored["body_hash"])
        view = {
            "schema_version": 1,
            "command_id": command_id,
            "command_hash": stored["command_hash"],
            "receipt_ref": receipt.to_json(),
            "subject_agent_id": body["subject_agent_id"],
            "subject_session_id": body["subject_session_id"],
            "outcome": body["outcome"],
            "result_ref": body["result_ref"],
            "result_revision": int(body["result_revision"]),
            "observed_at_ms": int(body["observed_at_ms"]),
        }
        return check("CommandReceiptView", view), body["result"]

    def _record_command(self, *, command_id: str, verb: str, subject_id: str, command_hash: str, subject_agent_id: str | None, subject_session_id: str | None, result_ref: Pin | None, result_revision: int, result: Any) -> dict[str, Any]:
        body = {
            "subject_agent_id": subject_agent_id, "subject_session_id": subject_session_id, "outcome": "APPLIED",
            "result_ref": None if result_ref is None else result_ref.to_json(), "result_revision": int(result_revision),
            "observed_at_ms": self._now_ms(), "result": plain(result),
        }
        with self.runtime.uow.database.transaction() as txn:
            store.put_host_command_locked(txn, command_id=command_id, verb=verb, subject_id=subject_id, command_hash=command_hash, body=body)
        stored = self._command_receipt(command_id)
        assert stored is not None
        return stored[0]

    def command_receipt_get(self, payload: Mapping[str, Any], *, subject_id: str) -> dict[str, Any]:
        value = check("ReceiptReadCommand", plain(payload))
        stored = self._command_receipt(str(value["command_id"]), subject_id=subject_id)
        if stored is not None and stored[0]["subject_session_id"] is not None:
            self._session(str(stored[0]["subject_session_id"]), subject_id=subject_id)
        if stored is None:
            raise ArpError("SOURCE_UNAVAILABLE", "no receipt for this command id")
        return stored[0]

    # ---- history search / read (management purposes) --------------------------------------------------

    def history_search(self, payload: Mapping[str, Any], *, caller: TrustedCaller, subject_id: str) -> dict[str, Any]:
        caller = self._caller(caller)
        session = self._live_session(subject_id)
        access = self.arp.retriever.access_for(session, purpose="MANAGEMENT_SEARCH", caller_ref=caller.principal_ref, turn_id=None)
        return check("ManagementSearchPage", self.arp.retriever.search(access, plain(payload)))

    def history_read(self, payload: Mapping[str, Any], *, caller: TrustedCaller, subject_id: str) -> dict[str, Any]:
        caller = self._caller(caller)
        session = self._live_session(subject_id)
        access = self.arp.retriever.access_for(session, purpose="MANAGEMENT_READ", caller_ref=caller.principal_ref, turn_id=None)
        return check("HistoryReadPage", self.arp.retriever.read(access, plain(payload)))

    def _live_session(self, agent_id: str) -> store.SessionRow:
        session = store.read_live_session(self.connection, agent_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "no live session for this agent")
        if session.state != "ACTIVE":
            raise ArpError("SESSION_DRAINING" if session.state == "DRAINING" else "SESSION_NOT_ACTIVE", f"session is {session.state}")
        return session

    # ---- catalogue / skills ------------------------------------------------------------------------------

    def catalogue_page(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = check("CatalogueReadCommand", plain(payload))
        if value["kind"] != kind:
            raise ArpError("CATALOGUE_KIND_MISMATCH", f"this verb lists {kind}")
        return self.catalogue.page(value, access_view="MANAGEMENT")

    def skill_details(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.arp.skills.details(payload)

    def skill_install(self, payload: Mapping[str, Any], *, caller: TrustedCaller, command_id: str) -> tuple[dict[str, Any], cat.RevisionRow, cat.ActivationRow]:
        caller = self._caller(caller)
        value = check("SkillInstallCommand", plain(payload))
        data = self._artifact_bytes(Pin.from_json(value["bundle_artifact_ref"]))
        result = self.arp.skills.import_bundle(value, data, caller=caller, command_id=command_id)
        return self._skill_item(result.revision), result.revision, result.activation

    # ---- envelope ---------------------------------------------------------------------------------------------

    def _payload(self, request: Mapping[str, Any]) -> Any:
        if request["payload"] is not None:
            if len(canonical(request["payload"])) > INLINE_PAYLOAD_MAX:
                raise ArpError("ITEM_TOO_LARGE", "inline payload exceeds 256KiB", field_path="$.payload")
            return request["payload"]
        ref = Pin.from_json(request["payload_ref"])
        raw = self._artifact_bytes(ref)
        payload = parse_strict(raw, max_bytes=INLINE_PAYLOAD_MAX * 4)
        # The resolved payload goes through exactly the inline envelope semantics
        # (request type, cursor / limit echo, settings revision pairing).
        check("HostRequest", {**plain(request), "payload": payload, "payload_ref": None})
        return payload

    @staticmethod
    def _expect(request: Mapping[str, Any], actual: int) -> None:
        if request["expected_revision"] != actual:
            raise ArpError("EXPECTED_REVISION_MISMATCH", f"expected_revision must be {actual}")

    async def handle(self, request: Mapping[str, Any], *, caller: TrustedCaller) -> dict[str, Any]:
        """One ``HostRequest`` → one ``HostResponse``; failures come back as the ``Error`` DTO."""

        verb = request.get("verb") if isinstance(request, Mapping) else None
        subject = request.get("subject_id") if isinstance(request, Mapping) else None
        response: dict[str, Any] = {
            "schema_version": 1, "request_verb": verb if verb in host_verbs() else "agent_context_summary", "subject_id": str(subject or "-"),
            "view_revision": 0, "as_of_context_id": None, "items": [], "next_cursor": None, "error": None, "command_receipt": None,
        }
        try:
            if verb not in host_verbs():
                raise ArpError("UNSUPPORTED_HOST_VERB", field_path="$.verb")
            value = check("HostRequest", plain(request))
            self._caller(caller)
            outcome = self._dispatch(value, caller)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            item, view_revision, receipt = outcome
            response.update(view_revision=int(view_revision), items=[item], next_cursor=item.get("next_cursor") if isinstance(item, Mapping) else None, command_receipt=receipt)
            if value["verb"] in SESSION_VERBS:
                session_id = self._subject_session_id(value)
                if session_id is not None:
                    latest = store.latest_context(self.connection, session_id)
                    response["as_of_context_id"] = None if latest is None else latest.context_id
            return check("HostResponse", response)
        except ArpError as error:
            response.update(items=[], command_receipt=None, error=error_json(error))
        except Exception as error:  # noqa: BLE001 - never leak an unknown exception's text
            code = "SESSION_NOT_ACTIVE" if type(error).__name__ in ("AgentNotFound", "AgentInputConflict", "AgentClosedError") else "STATE_COMBINATION_INVALID"
            response.update(items=[], command_receipt=None, error=error_json(ArpError(code, type(error).__name__)))
        return check("HostResponse", response)

    def _subject_session_id(self, value: Mapping[str, Any]) -> str | None:
        payload = value["payload"]
        if isinstance(payload, Mapping) and isinstance(payload.get("session_id"), str):
            return str(payload["session_id"])
        session = store.read_live_session(self.connection, str(value["subject_id"]))
        return None if session is None else session.session_id

    def _write_guard(self, value: Mapping[str, Any]) -> str:
        command_id = value["command_id"]
        if not isinstance(command_id, str) or not command_id.strip():
            raise ArpError("MISSING_FIELD", field_path="$.command_id")
        if value["expected_revision"] is None:
            raise ArpError("MISSING_FIELD", field_path="$.expected_revision")
        return command_id

    def _replayed(self, value: Mapping[str, Any], command_hash: str) -> tuple[Any, int, dict[str, Any]] | None:
        stored = self._command_receipt(str(value["command_id"]), subject_id=str(value["subject_id"]))
        if stored is None:
            return None
        receipt, result = stored
        if receipt["command_hash"] != command_hash:
            raise ArpError("EXPECTED_REVISION_MISMATCH", "command id reused with another body")
        if value["verb"] == "agent_context_policy_submit":
            result = check("PolicySubmissionView", {**result, "command_receipt": receipt})
        return result, receipt["result_revision"], receipt

    def _dispatch(self, value: Mapping[str, Any], caller: TrustedCaller):  # type: ignore[no-untyped-def]
        verb = str(value["verb"])
        subject = str(value["subject_id"])
        payload = self._payload(value)
        entry = host_verbs()[verb]
        if entry["access"] == "READ":
            return self._read(verb, subject, payload, caller)
        command_id = self._write_guard(value)
        command_hash = digest({"verb": verb, "subject_id": subject, "payload": payload, "caller": caller.to_json()})
        replayed = self._replayed(value, command_hash)
        if replayed is not None:
            return replayed
        return self._write(verb, subject, payload, caller, command_id, command_hash, value)

    def _read(self, verb: str, subject: str, payload: Any, caller: TrustedCaller) -> tuple[Any, int, None]:
        if verb == "agent_context_summary":
            item = self.context_summary(payload, subject_id=subject)
            return item, self._context_eventseq(item["context_id"], item["session_id"]) if item["context_id"] else 0, None
        if verb == "agent_context_history":
            item = self.context_history(payload, subject_id=subject)
            return item, self._context_eventseq(None, str(payload["session_id"])), None
        if verb == "agent_context_manifest":
            item = self.context_manifest(payload, subject_id=subject)
            return item, self._context_eventseq(item["context_ref"]["id"], str(payload["session_id"])), None
        if verb == "agent_context_settings_get":
            item = self.settings_get(payload, subject_id=subject)
            return item, item["adoption_revision"], None
        if verb == "agent_context_policy_get":
            return self.policy_get(payload), int(payload["policy_ref"]["revision"]), None
        if verb == "agent_command_receipt_get":
            item = self.command_receipt_get(payload, subject_id=subject)
            return item, item["result_revision"], None
        if verb == "agent_session_history_search":
            item = self.history_search(payload, caller=caller, subject_id=subject)
            return item, int(item["receipt"]["index_upper_commit"]), None
        if verb == "agent_session_history_read":
            item = self.history_read(payload, caller=caller, subject_id=subject)
            return item, int(item["journal_highwater"]), None
        if verb in ("agent_capabilities_list", "agent_tool_catalogue", "agent_skills_list"):
            kind = {"agent_capabilities_list": "CAPABILITY", "agent_tool_catalogue": "TOOL", "agent_skills_list": "SKILL"}[verb]
            self._catalogue_subject(subject)
            item = self.catalogue_page(kind, payload)
            return item, int(item["registry_epoch"]), None
        if verb == "agent_skill_details":
            self._catalogue_subject(subject)
            item = self.skill_details(payload)
            return item, int(item["skill_ref"]["revision"]), None
        raise ArpError("UNSUPPORTED_HOST_VERB")

    def _owner_replayed(self, verb: str, command_id: str, *, session: store.SessionRow | None = None) -> bool:
        """Owner-side evidence that this command already applied (crash between the owner's
        write and the host receipt): the retry replays through the owner instead of hitting
        the moved revision fence.  Suspend / resume / retire have no ledger table: their
        evidence is the activation's latest step naming this command (``_skill_step_replayed``)."""

        connection = self.connection
        if verb == "agent_session_destroy":
            return session is not None and session.destroy_command_id == command_id
        if verb == "agent_session_destroy_resume":
            return store.read_original_receipt(connection, kind="session_destroy_resume", receipt_key=command_id) is not None
        if verb == "agent_session_rebuild":
            return store.read_original_receipt(connection, kind="session_rebuild", receipt_key=command_id) is not None
        ns = self.catalogue.namespace_id
        if verb == "agent_skill_install":
            return connection.execute("SELECT 1 FROM arp_skill_import_commands WHERE namespace_id=? AND command_id=?", (ns, command_id)).fetchone() is not None
        if verb == "agent_skill_trial":
            return connection.execute("SELECT 1 FROM arp_skill_evaluations WHERE namespace_id=? AND command_id=?", (ns, command_id)).fetchone() is not None
        if verb == "agent_skill_admit":
            return connection.execute("SELECT 1 FROM arp_skill_admissions WHERE namespace_id=? AND command_id=?", (ns, command_id)).fetchone() is not None
        return False

    def _skill_step_replayed(self, verb: str, activation: cat.ActivationRow, caller: TrustedCaller, command_id: str) -> bool:
        state = _SKILL_STEP_TARGET.get(verb)
        return state is not None and self.catalogue.applied_by(activation, state=state, caller=caller, command_id=command_id)

    def _catalogue_subject(self, subject: str) -> None:
        if subject != self.catalogue.namespace_id:
            raise ArpError("REF_OUTSIDE_SCOPE", "catalogue verbs use the authenticated namespace as subject")

    async def _write(self, verb: str, subject: str, payload: Any, caller: TrustedCaller, command_id: str, command_hash: str, value: Mapping[str, Any]):  # type: ignore[no-untyped-def]
        sessions = self.arp.sessions
        if verb == "agent_context_policy_submit":
            item_core, row = self.policy_submit(payload, caller=caller, command_id=command_id)
            receipt = self._record_command(command_id=command_id, verb=verb, subject_id=subject, command_hash=command_hash, subject_agent_id=None, subject_session_id=None, result_ref=row.pin, result_revision=row.revision, result=item_core)
            item = check("PolicySubmissionView", {**item_core, "command_receipt": receipt})
            return item, row.revision, receipt
        if verb == "agent_context_settings_update":
            session = self._session(str(payload["session_id"]), subject_id=subject)
            item = self.settings_update(payload, caller=caller, command_id=command_id, subject_id=subject)
            receipt = self._record_command(command_id=command_id, verb=verb, subject_id=subject, command_hash=command_hash, subject_agent_id=session.agent_id, subject_session_id=session.session_id, result_ref=Pin.from_json(item["effective_policy_ref"]), result_revision=item["adoption_revision"], result=item)
            return item, item["adoption_revision"], receipt
        if verb in ("agent_session_destroy", "agent_session_rebuild", "agent_session_destroy_resume"):
            session = self._session(str(payload["session_id"]), subject_id=subject)
            if not self._owner_replayed(verb, command_id, session=session):
                self._expect(value, session.row_version)
            if verb == "agent_session_destroy":
                if payload["command_id"] != command_id:
                    raise ArpError("SESSION_IDENTITY_MISMATCH", "payload command id differs from the envelope")
                after = await sessions.destroy(payload, caller=caller, command_id=command_id)
            elif verb == "agent_session_rebuild":
                after = sessions.rebuild(payload, caller=caller, command_id=command_id)
            else:
                if payload["command_id"] != command_id:
                    raise ArpError("SESSION_IDENTITY_MISMATCH", "payload command id differs from the envelope")
                after = sessions.resume_destroy(payload, caller=caller, command_id=command_id)
            item = sessions.view(after.session_id)
            receipt = self._record_command(command_id=command_id, verb=verb, subject_id=subject, command_hash=command_hash, subject_agent_id=after.agent_id, subject_session_id=after.session_id, result_ref=after.pin, result_revision=after.row_version, result=item)
            return item, after.row_version, receipt
        self._catalogue_subject(subject)
        if verb == "agent_skill_install":
            if not self._owner_replayed(verb, command_id):
                self._expect(value, int(payload["expected_catalogue_revision"]))
            item, revision, activation = self.skill_install(payload, caller=caller, command_id=command_id)
            receipt = self._record_command(command_id=command_id, verb=verb, subject_id=subject, command_hash=command_hash, subject_agent_id=None, subject_session_id=None, result_ref=revision.pin, result_revision=activation.row_version, result=item)
            return item, activation.row_version, receipt
        lifecycle = self.arp.lifecycle
        revision = self._skill(payload["skill_ref"])
        activation = cat.read_activation(self.connection, self.catalogue.namespace_id, "SKILL", revision.entry_id, revision.revision)
        if activation is None:
            raise ArpError("CATALOGUE_STALE", "skill activation row missing")
        if not (self._owner_replayed(verb, command_id) or self._skill_step_replayed(verb, activation, caller, command_id)):
            self._expect(value, activation.row_version)
        if verb == "agent_skill_trial":
            binding = lifecycle.begin_trial(payload, caller=caller, command_id=command_id)
            after = cat.read_activation(self.connection, self.catalogue.namespace_id, "SKILL", revision.entry_id, revision.revision)
            assert after is not None
            item = check("SkillEvaluationBinding", dict(binding))
            receipt = self._record_command(command_id=command_id, verb=verb, subject_id=subject, command_hash=command_hash, subject_agent_id=None, subject_session_id=None, result_ref=revision.pin, result_revision=after.row_version, result=item)
            return item, after.row_version, receipt
        if verb == "agent_skill_admit":
            after = lifecycle.admit(payload, caller=caller, command_id=command_id)
        elif verb == "agent_skill_suspend":
            after = lifecycle.suspend(payload, caller=caller, command_id=command_id)
        elif verb in ("agent_skill_resume", "agent_skill_retire"):
            expected_action = "RESUME" if verb == "agent_skill_resume" else "RETIRE"
            if payload["action"] != expected_action:
                raise ArpError("ENUM", f"this verb performs {expected_action}", field_path="$.action")
            after = lifecycle.transition(payload, caller=caller, command_id=command_id)
        else:
            raise ArpError("UNSUPPORTED_HOST_VERB")
        item = self._skill_item(revision)
        receipt = self._record_command(command_id=command_id, verb=verb, subject_id=subject, command_hash=command_hash, subject_agent_id=None, subject_session_id=None, result_ref=revision.pin, result_revision=after.row_version, result=item)
        return item, after.row_version, receipt


_SKILL_STEP_TARGET = {"agent_skill_suspend": "SUSPENDED", "agent_skill_resume": "ADMITTED", "agent_skill_retire": "RETIRED"}


def digest_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


__all__ = ("COMMAND_RECEIPT_KIND", "INLINE_PAYLOAD_MAX", "RuntimePlaneService", "error_json")
