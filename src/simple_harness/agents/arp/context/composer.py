# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``NativeContextComposer`` behind the original ``ContextPort`` and ``request_preparer`` (§3.2, §4).

Two moments, both on the original seams:

* ``ArpContextPort.load`` (the driver's ``ContextPort``) returns the **required**
  content only: instructions plus the current input anchor and any open tool
  tail, at the Journal high-water that is the CAS anchor.
* ``ArpProviderWire.prepare_request`` (the original ``request_preparer``, called
  before admission and physical handoff) runs the composer: it freezes the
  ``ProtocolGroupSnapshot``, drives the automatic recall to READY / SKIPPED,
  allocates A–E + F + G with the single §4.1 algorithm, renders the request,
  restores the issued tool calls from the effects ledger, meters the final wire
  with the real counter, shrinks at most 256 times if the final count overflows,
  and freezes the ``ContextManifest`` (v2) in ``arp_context_requests`` together
  with the ``RuntimeContextPrepared`` event.  A replay of the same request key
  re-renders the frozen manifest and must reproduce the same request hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from simple_harness.contracts import RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.base_agent import AgentJournalRecord
from simple_harness.providers import ProviderRequest
from simple_harness.providers.errors import ProviderRequestRejectedError
from simple_harness.runtime.context import ContextSnapshot

from ...context.composer import _message_of
from ...context.port import JournalContextPort
from ...context.tokenizer import count_message, count_tools
from ...wire import AgentProviderWire, restore_wire_messages, run_id_from_request
from .. import store
from ..codec import check
from ..catalogue import ToolExposureService, read_tool_snapshot
from ..errors import ArpError
from ..pins import Pin
from ..rules import Budget, Group, Recall, Selection, Turn, allocate
from ..search import SessionAccess, _text_of
from ..strict import digest, plain
from .skill_blocks import SkillBlock, replay_skill_blocks, skill_blocks_for, skill_message
from . import groups as protocol_groups
from .recall import RecallSources, limits_for

MAX_SHRINK_STEPS = 256
_RECALL_TAG = re.compile(r"<(/?)recalled_history", re.IGNORECASE)
TRUST_OF_KIND = {
    "USER_ANCHOR": "USER_INPUT",
    "CLOSED_TOOL": "TOOL_RESULT",
    "OPEN_TAIL": "TOOL_RESULT",
    "TERMINAL_ANSWER": "AGENT_CLAIM",
    "OPAQUE_REQUIRED": "RAW_DIALOGUE",
    "HISTORY_MESSAGE": "RAW_DIALOGUE",
}


def recall_message(item: Mapping[str, Any], text: str) -> Message:
    """Recalled history is data inside an untrusted frame, never a new instruction."""

    body = _RECALL_TAG.sub(r"&lt;\1recalled_history", text)
    return Message(
        MessageRole.USER,
        f'<recalled_history chunk="{item["chunk_id"]}" provenance="{item["provenance"]}" untrusted="true">\n'
        f"{body}\n</recalled_history>",
        metadata={"derived": True, "recalled_chunk_id": item["chunk_id"], "rank_ordinal": int(item["rank_ordinal"])},
    )


@dataclass(slots=True)
class PreparedContext:
    """Everything one prepare slot decided, kept in memory until the manifest is frozen."""

    highwater: int
    turn_id: str
    snapshot: protocol_groups.GroupSnapshot
    session: store.SessionRow
    selection: Selection
    recall_result: Mapping[str, Any]
    recall_texts: Mapping[str, str]
    budget: Budget
    fixed: int
    messages: tuple[Message, ...]
    tool_snapshot: Mapping[str, Any] | None = None
    tool_witnesses: tuple[Pin, ...] = ()
    skill_blocks: tuple[SkillBlock, ...] = ()


class ArpContextPort(JournalContextPort):
    """Journal port whose ``load`` is required-only and whose composer runs at prepare."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.arp: Any = None
        self.prepared: list[str] = []
        self.native_loads = 0

    def bind(self, arp: Any) -> None:
        self.arp = arp

    # ---- helpers ------------------------------------------------------------------------

    def _connection(self):  # type: ignore[no-untyped-def]
        return self._uow.database.connection

    def _session(self, agent_id: str) -> store.SessionRow:
        session = store.read_live_session(self._connection(), agent_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "no live ARP session for this agent")
        if session.state != "ACTIVE":
            raise ArpError("SESSION_DRAINING" if session.state == "DRAINING" else "SESSION_NOT_ACTIVE", session.state)
        return session

    def _capture(self, session: store.SessionRow, highwater: int) -> protocol_groups.GroupSnapshot:
        return protocol_groups.capture(
            self._connection(),
            session_id=session.session_id,
            agent_id=session.agent_id,
            highwater=highwater,
            charge=self._count,
            session_ref=session.pin,
        )

    def _access(self, session: store.SessionRow, turn_id: str) -> SessionAccess:
        arp = self.arp
        authority = arp.policy.approval_ref
        return SessionAccess(
            session_ref=session.pin,
            agent_ref=Pin("agent", session.agent_id, 0, digest(session.agent_id)),
            turn_ref=Pin("agent_turn", turn_id, 0, digest(turn_id)),
            root_incarnation=session.root_incarnation,
            purpose="CONTEXT_RECALL",
            caller_ref=Pin("principal", "runtime:context-composer", 0, digest("runtime:context-composer")),
            authority_refs=(authority,),
            authority_readset_hash=digest({"authority": authority.to_json(), "profile": session.profile_ref.to_json(), "session": session.pin.to_json()}),
            control_generation=session.generation,
            expires_at_ms=arp.ports.clock_ms() + int(arp.policy_for(session)[1].body["query_cursor_ttl_ms"]),
        )

    @staticmethod
    def _group_messages(group: protocol_groups.GroupView) -> list[Message]:
        return [_message_of(r) for r in group.visible_records]

    # ---- ContextPort: required content only ------------------------------------------------

    def load(self, run_id: RunId) -> ContextSnapshot:
        agent_id = run_id.value
        self.loads += 1
        self.native_loads += 1
        highwater = self._uow.agent_journal_highwater(agent_id)
        if highwater == 0:
            return ContextSnapshot(0, ())
        session = self._session(agent_id)
        snapshot = self._capture(session, highwater)
        messages: list[Message] = [_message_of(r) for r in snapshot.instructions if r.visibility == "context"]
        for group in snapshot.groups:
            if group.mandatory:
                messages.extend(self._group_messages(group))
        return ContextSnapshot(highwater, tuple(messages))

    # ---- composer at prepare ------------------------------------------------------------------

    def freeze(self, run_id: str, request: ProviderRequest) -> ProviderRequest:
        arp = self.arp
        if arp is None:
            raise ArpError("STATE_COMBINATION_INVALID", "context port not bound to the runtime plane")
        request_key = request.request_id.value
        _, _, ordinal_text = request_key.rpartition(":provider-turn:")
        if not ordinal_text.isdigit():
            raise ArpError("REQUEST_PHASE_UNMAPPED", "request id carries no provider turn ordinal")
        ordinal = int(ordinal_text)
        if request.max_output_tokens is None:
            raise ArpError("INVALID_OUTPUT_RESERVE", "ARP requests must carry max_output_tokens")
        connection = self._connection()
        session = self._session(run_id)
        frozen = store.read_context_by_request_key(connection, request_key)
        highwater = self._uow.agent_journal_highwater(run_id)
        if frozen is not None:
            return self._replay(frozen, session, request)
        request, tool_snapshot, tool_witnesses = self._expose(session, request)
        snapshot = self._capture(session, highwater)
        turn_id = snapshot.current_turn_id
        turn = self._uow.read_agent_turn(turn_id)
        if turn is None:
            raise ArpError("STATE_COMBINATION_INVALID", "current turn row missing")
        adoption = store.latest_adoption(connection, session.session_id)
        if adoption is None:
            raise ArpError("POLICY_CONFLICT", "session has no adopted context policy")
        # RP-D2: the Session's *adopted* policy governs every request frozen after the
        # adoption (settings update = next request only); the profile policy is its origin.
        # A request whose recall was already coordinated (C0 done, crash, re-sent prepare)
        # keeps the policy / adoption frozen in that request, never today's.
        existing_recall = store.get_context_recall_exact(connection, request_key)
        adoption_revision = adoption.adoption_revision
        policy_ref = adoption.policy_ref
        if existing_recall is not None:
            adoption_revision = int(existing_recall.request["adoption_revision"])
            policy_ref = Pin.from_json(existing_recall.request["policy_ref"])
        policy_row = store.read_policy_object(connection, policy_ref.id, policy_ref.revision)
        if policy_row is None or policy_row.content_hash != policy_ref.content_hash:
            raise ArpError("POLICY_CONFLICT", "adopted policy object is missing or differs")
        policy = policy_row.body
        limits = arp.meter.binding.model_limits
        prior = arp.meter.binding.prior_for(run_id)
        # Index: enqueue closed groups (short exec txn) and run due jobs in-band.
        state = arp.index.state_for(session)
        turn_ref = Pin("agent_turn", turn_id, 0, digest(turn_id))
        with self._uow.database.transaction() as txn:
            arp.index.enqueue_closed_groups_locked(
                txn, session=session, snapshot=snapshot, generation=state.generation, authority_ref=arp.policy.approval_ref, turn_ref=turn_ref
            )
        if not arp.index.background_embedding:
            # A heavy embedding model runs in the background pump, never inside a Turn:
            # groups closed moments ago are still in the recent window.
            arp.index.process_due(session_id=session.session_id)
        # Budget (U_wire) with the real deployment limits and the request's own output cap.
        budget = Budget(
            configured_total=int(policy["max_context_tokens"]),
            model_total=limits["combined_limit_tokens"],
            model_input=int(limits["input_limit_tokens"]),
            model_output=int(limits["max_output_tokens"]),
            output=int(request.max_output_tokens),
            safety=int(policy["safety_reserve_tokens"]),
            headroom=int(policy["tool_headroom_tokens"]),
            recent_floor=int(policy["recent_min_tokens"]),
            recall_ceiling=int(policy["recall_max_tokens"]),
            prior=prior.tokens,
            input_scope=str(limits["input_limit_scope"]),
        )
        instructions = [r for r in snapshot.instructions if r.visibility == "context"]
        tool_tokens = self._fixed_tool_charge(request, instructions)
        skill_blocks = skill_blocks_for(arp, session, count=arp.index.count)
        fixed = sum(self._count(r) for r in instructions) + tool_tokens + sum(b.tokens for b in skill_blocks)
        rule_groups = [
            Group(g.group_id, g.turn_id, g.seq_from, g.budget_charge, g.kind, g.mandatory, g.closed, g.call_ids, g.result_call_ids)
            for g in snapshot.groups
        ]
        rule_turns = [Turn(t["turn_id"], bool(t["completed"]), frozenset(t["required_group_ids"])) for t in snapshot.body["turns"]]
        # Mandatory + protected recent suffix are excluded from recall before the query starts.
        pre = allocate(budget, fixed, rule_groups, (), turns=rule_turns, current_turn_id=turn_id, enumeration_complete=bool(snapshot.body["enumeration_complete"]))
        protected = tuple(g.id for g in pre.recent if not g.mandatory)
        access = self._access(session, turn_id)
        clock_ref = self._clock_receipt(session, request_key)
        if existing_recall is not None:
            # C0 already happened for this request key (crash after C0, or a re-sent
            # prepare): continue the frozen request; never build a second one.
            if existing_recall.session_id != session.session_id or int(existing_recall.request["journal_highwater"]) != highwater:
                raise ArpError("RECALL_SOURCE_STALE", "request key re-sent against another journal state")
            result = arp.recall.resume(existing_recall.recall_key, access, now_ms=arp.ports.clock_ms(), clock_receipt_ref=clock_ref)
            recall_request = existing_recall.request
        else:
            query_parts = self._query_parts(snapshot, turn_id, highwater)
            sources = RecallSources(
                access=access,
                session=session,
                turn_id=turn_id,
                original_request_key=request_key,
                provider_request_ordinal=ordinal,
                policy_ref=policy_row.pin,
                adoption_revision=adoption_revision,
                source_snapshot_ref=Pin("artifact", f"source-snapshot:{session.session_id}:{highwater}", highwater, snapshot.snapshot_hash),
                source_snapshot_hash=snapshot.snapshot_hash,
                group_snapshot_ref=snapshot.pin,
                journal_highwater=highwater,
                expected_group_set_hash=snapshot.expected_group_set_hash,
                expected_groups=sum(1 for g in snapshot.groups if g.indexable),
                mandatory_group_ids=tuple(snapshot.body["mandatory_group_ids"]),
                protected_group_ids=protected,
                query_parts=query_parts,
                embedding_resource_ref=(
                    None if arp.index.embedding is None
                    else Pin("provider", f"embedding:{arp.index.embedding_fingerprint[:16]}", 1, digest(arp.index.embedding_resource_ref.to_json()))
                ),
                embedding_fingerprint=None if arp.index.embedding is None else arp.index.embedding_fingerprint,
                allow_lexical_degradation=bool(arp.ports.profile.allow_lexical_degradation),
                limits=limits_for(policy),
            clock_receipt_ref=clock_ref,
            )
            recall_request = arp.recall.build_request(sources)
            result = arp.recall.start(access, recall_request)
        candidates: list[Recall] = []
        texts: dict[str, str] = {}
        if result["outcome"] == "READY":
            generation = state.generation.index_generation
            service = arp.search_for(session)[0]
            for item in result["candidate_items"]:
                text = service.chunk_text(generation, item["chunk_id"])
                texts[item["chunk_id"]] = text
                charge = count_message(self._tokenizer, recall_message(item, text))
                candidates.append(Recall(item["chunk_id"], frozenset(item["source_group_ids"]), charge))
        candidates = candidates[: int(policy["max_recall_items"])]
        selection = allocate(budget, fixed, rule_groups, candidates, turns=rule_turns, current_turn_id=turn_id, enumeration_complete=bool(snapshot.body["enumeration_complete"]))
        prepared = PreparedContext(highwater, turn_id, snapshot, session, selection, result, texts, budget, fixed, (), tool_snapshot, tool_witnesses, skill_blocks)
        wire, measurement = self._render_and_measure(prepared, request, instructions)
        self._write_manifest(prepared, wire, measurement, request_key, ordinal, adoption_revision, tool_tokens, instructions, policy_row=policy_row)
        self.prepared.append(request_key)
        return wire

    # ---- rendering / metering ------------------------------------------------------------------

    def _fixed_tool_charge(self, request: ProviderRequest, instructions: Sequence[AgentJournalRecord]) -> int:
        """The tool block's charge in the plan.  With an exact rendered-request counter the
        fixed part (instructions + tool schemas + the template's request framing and
        tool-calling preamble) is measured on that same counter — a skeleton request with no
        history — so the plan and the final measurement agree.  A generic word heuristic
        would under-charge it (the real DeepSeek template adds ~270 tokens) and step 8 would
        then drop the recall first on every full window."""

        heuristic = count_tools(self._tokenizer, tuple(request.tools))
        exact = getattr(self.arp.meter.binding.tokenizer, "count_request_tokens", None)
        if not callable(exact) or not instructions:
            return heuristic
        skeleton = replace(request, messages=tuple(_message_of(r) for r in instructions))
        measured = self.arp.meter.count_wire(skeleton) - sum(self._count(r) for r in instructions)
        return max(heuristic, measured)

    def _render(self, prepared: PreparedContext, request: ProviderRequest, instructions: Sequence[AgentJournalRecord]) -> ProviderRequest:
        by_id = {item["chunk_id"]: item for item in prepared.recall_result.get("candidate_items", [])}
        messages: list[Message] = [_message_of(r) for r in instructions]
        messages.extend(skill_message(b) for b in prepared.skill_blocks)
        for recall in prepared.selection.recalled:
            messages.append(recall_message(by_id[recall.id], prepared.recall_texts[recall.id]))
        for group in prepared.selection.recent:
            messages.extend(self._group_messages(prepared.snapshot.group(group.id)))
        restored, _ = restore_wire_messages(
            tuple(messages), self._connection(), prepared.session.agent_id, replay_reasoning=self.replay_reasoning
        )
        return replace(request, messages=restored)

    def _render_and_measure(self, prepared: PreparedContext, request: ProviderRequest, instructions: Sequence[AgentJournalRecord]):  # type: ignore[no-untyped-def]
        arp = self.arp
        for _ in range(MAX_SHRINK_STEPS + 1):
            wire = self._render(prepared, request, instructions)
            try:
                measurement = arp.meter.measure(
                    wire, run_id=prepared.session.agent_id, max_input_budget=prepared.selection.input_budget,
                    requested_output_tokens=int(request.max_output_tokens),
                )
            except ArpError as error:
                if error.code != "FINAL_CONTEXT_OVERFLOW":
                    raise
                shrunk = _shrink(prepared.selection)
                if shrunk is None:
                    raise ArpError("REQUIRED_CONTEXT_TOO_LARGE", "mandatory content alone exceeds the final budget") from error
                prepared.selection = shrunk
                continue
            prepared.messages = wire.messages
            return wire, measurement
        raise ArpError("CONTEXT_ASSEMBLY_LIMIT", "no fitting request within 256 reductions")

    def _write_manifest(self, prepared: PreparedContext, wire: ProviderRequest, measurement: Any, request_key: str, ordinal: int, adoption_revision: int, tool_tokens: int, instructions: Sequence[AgentJournalRecord], *, policy_row: store.PolicyObjectRow | None = None) -> None:
        arp = self.arp
        session = prepared.session
        policy_row = policy_row or arp.policy
        policy = policy_row.body
        selection = prepared.selection
        snapshot = prepared.snapshot
        result = prepared.recall_result
        context_id = "ctx-" + digest({"request": request_key, "session": session.session_id})[:32]
        tool_snapshot_hash = digest([{"name": t.name, "description": t.description, "parameters": plain(t.parameters)} for t in wire.tools])
        sections: list[dict[str, Any]] = []
        if instructions:
            sections.append(
                {
                    "section": "A", "block_id": "instructions", "source_refs": [Pin("journal_record", r.record_id, r.seq, r.content_hash).to_json() for r in instructions],
                    "view_ref": Pin("artifact", "view:instructions", 0, digest([[r.record_id, r.content_hash] for r in instructions])).to_json(),
                    "trust": "CONTROL", "required": True, "budget_charge": sum(self._count(r) for r in instructions), "validity_witness_ref": None,
                }
            )
        if wire.tools:
            sections.append(
                {
                    "section": "E", "block_id": "tool-schemas", "source_refs": [Pin("receipt", f"tools:{session.agent_id}", 0, tool_snapshot_hash).to_json()],
                    "view_ref": Pin("artifact", "view:tools", 0, tool_snapshot_hash).to_json(),
                    "trust": "CONTROL", "required": True, "budget_charge": tool_tokens, "validity_witness_ref": None,
                }
            )
        for block in prepared.skill_blocks:
            sections.append(
                {
                    "section": "E", "block_id": block.block_id, "source_refs": [block.receipt_ref.to_json(), block.artifact_ref.to_json()],
                    "view_ref": block.artifact_ref.to_json(), "trust": "SKILL_INSTRUCTIONS", "required": True, "budget_charge": block.tokens, "validity_witness_ref": None,
                }
            )
        by_id = {item["chunk_id"]: item for item in result.get("candidate_items", [])}
        for recall in selection.recalled:
            item = by_id[recall.id]
            sections.append(
                {
                    "section": "F", "block_id": f"recall:{recall.id}", "source_refs": list(item["source_record_refs"]),
                    "view_ref": item["view_ref"], "trust": item["provenance"], "required": False, "budget_charge": recall.tokens, "validity_witness_ref": None,
                }
            )
        for group in selection.recent:
            view = snapshot.group(group.id)
            sections.append(
                {
                    "section": "G", "block_id": f"group:{group.id}", "source_refs": [p.to_json() for p in view.record_refs],
                    "view_ref": Pin("artifact", f"view:{group.id}", 0, view.view_hash).to_json(), "trust": TRUST_OF_KIND[view.kind],
                    "required": bool(view.mandatory), "budget_charge": group.tokens, "validity_witness_ref": None,
                }
            )
        publication = store.active_index_publication(self._connection(), session.session_id)
        read_set = {
            "journal_highwater": prepared.highwater,
            "group_snapshot": snapshot.snapshot_hash,
            "policy": policy_row.pin.to_json(),
            "adoption_revision": adoption_revision,
            "tool_snapshot": tool_snapshot_hash,
            "recall_result": digest(result),
            "skill_blocks": [[b.block_id, b.artifact_ref.content_hash] for b in prepared.skill_blocks],
        }
        manifest = {
            "schema_version": 2,
            "context_id": context_id,
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "turn_id": prepared.turn_id,
            "provider_request_ordinal": ordinal,
            "session_generation": session.generation,
            "profile_ref": session.profile_ref.to_json(),
            "model_limits": dict(arp.meter.binding.model_limits),
            "owner_contract_ref": Pin("policy", f"{session.profile_id}:owner-mode:{arp.ports.profile.owner_mode}", session.profile_revision, session.profile_hash).to_json(),
            "journal_highwater": prepared.highwater,
            "sections": sections,
            "recent_group_ids": [g.id for g in selection.recent],
            "recent_complete_turn_count": selection.recent_complete_turns,
            "recalled_chunk_ids": [r.id for r in selection.recalled],
            "retrieval_receipt_ref": Pin("retrieval", result["recall_key"], 0, digest(result)).to_json(),
            "tool_snapshot_ref": (
                Pin("tool_snapshot", f"{session.agent_id}:tools", 0, tool_snapshot_hash) if prepared.tool_snapshot is None
                else ToolExposureService.snapshot_pin(prepared.tool_snapshot)
            ).to_json(),
            "skill_refs": [p.to_json() for p in dict.fromkeys(b.skill_ref for b in prepared.skill_blocks)],
            "planned_request_hash": measurement.request_hash,
            "input_token_charge": measurement.wire_tokens,
            "effective_input_budget": selection.input_budget,
            "reserved_output_tokens": int(request_output(wire)),
            "safety_reserve_tokens": int(policy["safety_reserve_tokens"]),
            "tool_headroom_tokens": int(policy["tool_headroom_tokens"]),
            "counter_mode": arp.meter.binding.count_mode,
            "registry_epoch": 0 if prepared.tool_snapshot is None else int(prepared.tool_snapshot["registry_epoch"]),
            "authority_refs": [policy_row.approval_ref.to_json()],
            "source_read_set_ref": Pin("artifact", f"readset:{context_id}", 0, digest(read_set)).to_json(),
            "creation_root_id": session.creation_root_id,
            "effective_context_policy_ref": policy_row.pin.to_json(),
            "token_receipt": dict(measurement.receipt),
            "prior_output_reserve_tokens": int(measurement.receipt["prior_output_reserve_tokens"]),
            "adoption_revision": adoption_revision,
            "index_generation_ref": None if publication is None else publication.pin.to_json(),
            "N_count_complete": bool(snapshot.body["enumeration_complete"]) and selection.count_complete,
            "selected_turn_ids": list(selection.complete_turn_ids),
            "group_snapshot_ref": snapshot.pin.to_json(),
            "catalogue_witness_refs": [w.to_json() for w in prepared.tool_witnesses],
        }
        value = check("ContextManifest", manifest)
        proof = Pin.from_json(measurement.receipt["proof_ref"])
        with self._uow.database.transaction() as txn:
            row = store.put_context_locked(txn, manifest=value, original_request_key=request_key, catalog_epoch=int(manifest["registry_epoch"]), source_read_set=read_set, now_ms=arp.ports.clock_ms())
            store.append_runtime_event_locked(
                txn,
                run_id=session.agent_id,
                event_type="RuntimeContextPrepared",
                body={
                    "context_ref": row.pin.to_json(),
                    "request_ref": Pin("invocation", request_key, ordinal, measurement.request_hash).to_json(),
                    "session_ref": session.pin.to_json(),
                    "source_receipt_ref": proof.to_json(),
                },
                source_receipt_ref=proof,
                dedupe_key=f"{request_key}:{row.manifest_hash}",
                now=self._clock(),
            )

    def replayed_request(self, request: ProviderRequest) -> tuple[store.ContextRequestRow, ProviderRequest] | None:
        """Read-only: the frozen manifest of ``request`` and the wire request it really sent.

        For readers that must prove what a finished request showed the model (the Assurance
        review import): no session state is required to be ACTIVE and nothing is written.
        None when no manifest was frozen for this request; ``REQUEST_HASH_MISMATCH`` when
        the durable sources no longer reproduce the planned hash (never a guess)."""

        connection = self._connection()
        frozen = store.read_context_by_request_key(connection, request.request_id.value)
        if frozen is None:
            return None
        session = store.read_session(connection, frozen.session_id)
        if session is None or session.agent_id != frozen.agent_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "frozen context names no such session")
        return frozen, self._replay(frozen, session, request)

    def _replay(self, frozen: store.ContextRequestRow, session: store.SessionRow, request: ProviderRequest) -> ProviderRequest:
        """Re-render the frozen manifest; the wire must reproduce the planned request hash."""

        arp = self.arp
        manifest = frozen.manifest
        frozen_tools = read_tool_snapshot(self._connection(), str(manifest["tool_snapshot_ref"]["id"]))
        if frozen_tools is not None:
            allowed = {str(t["model_name"]) for t in frozen_tools["tools"]}
            request = replace(request, tools=tuple(t for t in request.tools if t.name in allowed))
        snapshot = self._capture(session, int(manifest["journal_highwater"]))
        instructions = [r for r in snapshot.instructions if r.visibility == "context"]
        messages: list[Message] = [_message_of(r) for r in instructions]
        messages.extend(skill_message(b) for b in replay_skill_blocks(arp, manifest, count=arp.index.count))
        if manifest["recalled_chunk_ids"]:  # the index is touched only when something was recalled
            generation = arp.index.state_for(session).generation.index_generation
            service = arp.search_for(session)[0]
            recall_row = store.read_context_recall(self._connection(), str(manifest["retrieval_receipt_ref"]["id"]))
            items = {} if recall_row is None or recall_row.result is None else {i["chunk_id"]: i for i in recall_row.result["candidate_items"]}
            for chunk_id in manifest["recalled_chunk_ids"]:
                messages.append(recall_message(items[chunk_id], service.chunk_text(generation, chunk_id)))
        for group_id in manifest["recent_group_ids"]:
            messages.extend(self._group_messages(snapshot.group(group_id)))
        restored, _ = restore_wire_messages(
            tuple(messages), self._connection(), session.agent_id, replay_reasoning=self.replay_reasoning
        )
        wire = replace(request, messages=restored)
        from simple_harness.execution.provider_invocations import provider_request_fingerprint

        if provider_request_fingerprint(wire) != frozen.planned_request_hash:
            raise ArpError("REQUEST_HASH_MISMATCH", "frozen manifest does not reproduce the wire request")
        return wire

    # ---- tool exposure (§8) ----------------------------------------------------------------------

    def _expose(self, session: store.SessionRow, request: ProviderRequest) -> tuple[ProviderRequest, Mapping[str, Any] | None, tuple[Pin, ...]]:
        """Freeze which of the request's tools the model may see: only catalogue-admitted,
        currently usable tools survive; the rest are dropped from this request (their
        reasons stay on the snapshot service), never silently exposed."""

        arp = self.arp
        exposure = getattr(arp, "exposure", None)
        if exposure is None or not request.tools:
            return request, None, ()
        snapshot, exposed, _refused = exposure.prepare(session, [t.name for t in request.tools], authority_refs=[arp.policy.approval_ref])
        allowed = {str(t["model_name"]) for t in snapshot["tools"]}
        filtered = replace(request, tools=tuple(t for t in request.tools if t.name in allowed))
        return filtered, snapshot, tuple(e.activation.witness for e in exposed)

    # ---- query parts / clock receipt ------------------------------------------------------------

    def _query_parts(self, snapshot: protocol_groups.GroupSnapshot, turn_id: str, highwater: int) -> dict[str, tuple[str, Pin]]:
        parts: dict[str, tuple[str, Pin]] = {}
        anchor = next((g for g in snapshot.groups if g.kind == "USER_ANCHOR" and g.turn_id == turn_id), None)
        if anchor is not None:
            record = anchor.records[0]
            parts["CURRENT_INPUT"] = (_text_of(record), Pin("journal_record", record.record_id, record.seq, record.content_hash))
        feedback = None
        for group in snapshot.groups:
            if group.turn_id != turn_id or group.seq_to > highwater:
                continue
            for record in group.records:
                if record.kind in ("tool_result", "feedback") and record.visibility == "context":
                    feedback = record
        if feedback is not None:
            parts["LATEST_FEEDBACK"] = (_text_of(feedback), Pin("journal_record", feedback.record_id, feedback.seq, feedback.content_hash))
        return parts

    def _clock_receipt(self, session: store.SessionRow, request_key: str) -> Pin:
        arp = self.arp
        observed = arp.ports.clock_ms()
        tick_id = f"prepare:{request_key}:{observed}"
        body = {"kind": "context-recall-clock-v1", "request_key": request_key, "tick_id": tick_id, "observed_wall_ms": observed, "previous_highwater_ms": 0}
        with self._uow.database.transaction() as txn:
            return store.append_original_receipt_locked(txn, run_id=session.agent_id, kind="recall_clock", receipt_key=tick_id, body=body, now=self._clock())


def request_output(request: ProviderRequest) -> int:
    return int(request.max_output_tokens or 0)


def _shrink(selection: Selection) -> Selection | None:
    """One §4.1 step-8 reduction: drop the last recalled item, else the oldest optional group."""

    if selection.recalled:
        dropped = selection.recalled[-1]
        return replace(selection, recalled=selection.recalled[:-1], used=selection.used - dropped.tokens)
    optional = [g for g in selection.recent if not g.mandatory]
    if not optional:
        return None
    oldest = optional[0]
    remaining = tuple(g for g in selection.recent if g.id != oldest.id)
    complete = tuple(t for t in selection.complete_turn_ids if t != oldest.turn_id)
    return replace(selection, recent=remaining, used=selection.used - oldest.tokens, complete_turn_ids=complete)


class ArpContextRejected(ProviderRequestRejectedError):
    """A definite, pre-call refusal of the request by the native composer (never retried blindly)."""

    __slots__ = ("arp_code", "detail")
    error_code = "arp_context_rejected"
    default_message = "The native context composer refused this request."

    def __init__(self, error: ArpError) -> None:
        super().__init__(public_message=f"{error.code}: {error}", retryable=False)
        self.arp_code = error.code
        self.detail = {"code": error.code, "field_path": error.field_path, "detail": plain(error.detail)}


class ArpProviderWire(AgentProviderWire):
    """The original wire with the composer on ``prepare_request`` (INTERFACES §1)."""

    def __init__(self, *args: Any, context: Callable[[], ArpContextPort], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._context = context  # late-bound: the wire is assembled before the Context port

    def prepare_request(self, request: ProviderRequest) -> ProviderRequest:
        run_id = run_id_from_request(request.request_id.value)
        if run_id is None:
            return super().prepare_request(request)
        try:
            return self._context().freeze(run_id, request)
        except ArpError as error:
            raise ArpContextRejected(error) from error
        except Exception as error:  # noqa: BLE001 - no model call happened: fail this request definitively
            raise ArpContextRejected(
                ArpError("STATE_COMBINATION_INVALID", f"{type(error).__name__}: {error}")
            ) from error


__all__ = ("ArpContextPort", "ArpContextRejected", "ArpProviderWire", "PreparedContext", "recall_message")
