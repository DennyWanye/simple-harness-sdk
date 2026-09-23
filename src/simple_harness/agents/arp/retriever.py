"""``SessionRetriever``: the model's and the Host's window into one Session's history
(INTERFACES §1 ``SessionRetriever.search / read``; HOST-DTOS §4; CONTEXT-SEARCH C3/C6).

Both entries run on the same frozen-index engine as the automatic recall
(``SessionSearchService``) but under their own cursor purposes: a model cursor is never
a management cursor and neither is the internal ``CONTEXT_RECALL`` one. The scope is
always the bound Session of the authenticated subject; a request never names a Session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import embedding_call, store
from .codec import check
from .errors import ArpError
from .partition import NO_EMBEDDING_FINGERPRINT
from .pins import Pin
from .search import SessionAccess
from .strict import digest

SEARCH_PURPOSES = {"MODEL_SEARCH", "MANAGEMENT_SEARCH"}
READ_PURPOSES = {"MODEL_READ", "MANAGEMENT_READ"}


@dataclass(slots=True)
class SessionRetriever:
    arp: Any  # ArpRuntime
    capture: Callable[[store.SessionRow, int], Any]  # (session, highwater) -> GroupSnapshot

    # ---- access ---------------------------------------------------------------------------

    def access_for(self, session: store.SessionRow, *, purpose: str, caller_ref: Pin, turn_id: str | None) -> SessionAccess:
        if purpose not in SEARCH_PURPOSES | READ_PURPOSES:
            raise ArpError("ENUM", field_path="purpose")
        arp = self.arp
        authority = arp.policy.approval_ref
        return SessionAccess(
            session_ref=session.pin,
            agent_ref=Pin("agent", session.agent_id, 0, digest(session.agent_id)),
            turn_ref=None if turn_id is None else Pin("agent_turn", turn_id, 0, digest(turn_id)),
            root_incarnation=session.root_incarnation,
            purpose=purpose,
            caller_ref=caller_ref,
            authority_refs=(authority,),
            authority_readset_hash=digest({"authority": authority.to_json(), "profile": session.profile_ref.to_json(), "session": session.pin.to_json()}),
            control_generation=session.generation,
            expires_at_ms=arp.ports.clock_ms() + int(arp.policy.body["query_cursor_ttl_ms"]),
        )

    def _session(self, agent_id: str) -> store.SessionRow:
        session = store.read_live_session(self.arp.index.uow.database.connection, agent_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "no live session for this agent")
        if session.state != "ACTIVE":
            raise ArpError("SESSION_DRAINING" if session.state == "DRAINING" else "SESSION_NOT_ACTIVE", f"session is {session.state}")
        return session

    # ---- search (C3) ----------------------------------------------------------------------

    def search(self, access: SessionAccess, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """One ``SearchRequest`` → one ``SearchPage`` / ``ManagementSearchPage``."""

        if access.purpose not in SEARCH_PURPOSES:
            raise ArpError("ENUM", field_path="purpose")
        value = check("SearchRequest", dict(request))
        session = self._session(access.agent_id)
        if session.session_id != access.session_id or session.generation != access.control_generation:
            raise ArpError("CURSOR_SCOPE_MISMATCH", "access does not match the live session")
        service, guard, generation = self.arp.search_for(session)
        query_hash = digest(value["query"])
        expect = {"query_hash": query_hash, "page_items": int(value["limit"]), "max_bytes": int(value["max_bytes"])}
        if value["cursor"] is not None:
            with guard.held():
                page, cursor = service.resume_frozen(access, str(value["cursor"]), expect=expect)
            return page
        # A fresh query: freeze the Session's index snapshot (highwater, full expected set).
        highwater = self.arp.index.uow.agent_journal_highwater(session.agent_id)
        snapshot_groups = self.capture(session, highwater)
        policy = self.arp.policy.body
        identity = {"purpose": access.purpose, "owner": access.owner_scope_hash, "query": query_hash, "issued_at_ms": self.arp.ports.clock_ms(), "nonce": digest(value)}
        query_id = f"{access.purpose.lower()}-{digest(identity)[:32]}"
        with guard.held():
            snapshot = service.freeze_snapshot(
                access,
                index_generation=generation.index_generation,
                journal_highwater=highwater,
                expected_group_set_hash=snapshot_groups.expected_group_set_hash,
                expected_groups=sum(1 for g in snapshot_groups.groups if g.indexable),
                source_snapshot_hash=snapshot_groups.snapshot_hash,
                embedding_fingerprint=generation.embedding_fingerprint,
                chunker_fingerprint=generation.chunker_fingerprint,
                view_policy_hash=generation.view_policy_hash,
                identity=identity,
            )
        vector = None
        vector_ref = None
        mode = "LEXICAL_ONLY"
        embedding = self.arp.index.embedding
        if embedding is not None and generation.embedding_fingerprint != NO_EMBEDDING_FINGERPRINT:
            # C3 step 3: one embedding call per query identity, persisted, reused on resume.
            call_key = f"{session.session_id}/{query_id}/{generation.embedding_fingerprint}"
            receipt = embedding_call.perform_call(
                self.arp.index.uow, run_id=session.agent_id, call_key=call_key, purpose="SESSION_QUERY", texts=[value["query"]],
                port=embedding, deployment_ref=self.arp.index.embedding_resource_ref or Pin("deployment", "embedding:local", 1, generation.embedding_fingerprint),
                clock=self.arp.index.clock, clock_ms=self.arp.ports.clock_ms, fault=self.arp.ports.fault, fault_name="search.before_embed",
            )
            if receipt["status"] == "SUCCEEDED":
                vector = receipt["output"][0]
                vector_ref = Pin("provider", f"embedding:{generation.embedding_fingerprint[:16]}", 1, digest(receipt["invocation_ref"]))
                mode = "HYBRID"
            elif not self.arp.ports.profile.allow_lexical_degradation:
                raise ArpError("EMBEDDING_UNAVAILABLE", "query embedding failed and lexical degradation is not allowed")
        internal = {
            "query_text": value["query"],
            "query_hash": query_hash,
            "mandatory_group_ids": [],
            "protected_group_ids": [],
            "journal_highwater": highwater,
            "policy_ref": self.arp.policy.pin.to_json(),
            "control_generation": session.generation,
            "limits": {
                "page_rows": int(policy["max_scan_rows_per_page"]),
                "channel_top_k": int(policy["max_query_candidates"]),
                "global_candidates": int(policy["max_candidates"]),
                "page_items": int(value["limit"]),
                "max_bytes": int(value["max_bytes"]),
            },
        }
        with guard.held():
            page, cursor = service.start_frozen(
                access, internal, snapshot, query_vector=vector, query_vector_ref=vector_ref, mode=mode, query_id=query_id, purpose=access.purpose
            )
        return page

    # ---- read (C6) ------------------------------------------------------------------------

    def read(self, access: SessionAccess, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """One ``HistoryReadRequest`` → one ``HistoryReadPage`` from the exact Journal."""

        if access.purpose not in READ_PURPOSES:
            raise ArpError("ENUM", field_path="purpose")
        value = check("HistoryReadRequest", dict(request))
        session = self._session(access.agent_id)
        if session.session_id != access.session_id or session.generation != access.control_generation:
            raise ArpError("CURSOR_SCOPE_MISMATCH", "access does not match the live session")
        service, guard, _generation = self.arp.search_for(session)
        uow = self.arp.index.uow
        highwater = uow.agent_journal_highwater(session.agent_id)
        source_hash = self.capture(session, highwater).snapshot_hash
        with guard.held():
            return service.read_history(
                access, uow.database.connection, value, purpose=access.purpose, journal_highwater=highwater, source_snapshot_hash=source_hash
            )


__all__ = ("READ_PURPOSES", "SEARCH_PURPOSES", "SessionRetriever")
