# SPDX-License-Identifier: Apache-2.0
"""Original executor identities, including service rehandoffs, without ID guessing.

Historical views below are read-only bindings, not new dispatch intents. They
retain the same original frozen request and account. A missing event or SDK
binding is a source failure, never permission to forget an earlier executor.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from simple_harness.agents.runtime import agent_id_for

from ..contracts.models import sha256_hex
from ..storage.store import DispatchIntent, Store
from .planning_operations import SourceUnavailable


def read_dispatch_history(store: Store, runtime: Any, intent: DispatchIntent) -> tuple[DispatchIntent, ...]:
    """Read the complete original event chain; no list_events pagination cutoff.

    The producer's AgentCreated/InputSubmitted events bind an old executor to
    this subject. Its SDK creation key is read from that binding, not recovered
    by splitting an Agent id or treating a provider grant as authorization.
    """
    with store.read_view() as db:
        current = store.get_intent(intent.intent_id)
        if current != intent:
            raise SourceUnavailable("dispatch_history_intent_changed")
        rows = db.execute(
            "SELECT seq,type,idempotency_key,payload_json FROM events WHERE mission_id=? "
            "AND type IN ('ServiceIntentRehandedOff','AgentCreated','InputSubmitted') ORDER BY seq",
            (intent.mission_id,),
        ).fetchall()
        events = [(row[0], row[1], row[2], json.loads(row[3])) for row in rows]
        if any(not isinstance(item[3], dict) for item in events):
            raise SourceUnavailable("dispatch_history_event_invalid")
        handoffs = [item for item in events if item[1] == "ServiceIntentRehandedOff"
                    and (item[3].get("subject_id") == intent.subject_id
                         or item[3].get("intent_id") == intent.intent_id)]
        if handoffs and intent.kind == "attempt":
            raise SourceUnavailable("dispatch_history_worker_rehandoff_invalid")
        result: list[DispatchIntent] = []
        seen: set[str] = set()
        previous_seq = 0
        for ordinal, (seq, _, key, payload) in enumerate(handoffs, start=1):
            agent_id, turn_id = payload.get("previous_agent_id"), payload.get("previous_turn_id")
            if (payload.get("intent_id") != intent.intent_id or payload.get("subject_id") != intent.subject_id
                    or payload.get("kind") != intent.kind or type(payload.get("rehandoff")) is not int
                    or payload["rehandoff"] != ordinal
                    or key != f"ServiceIntentRehandedOff:{intent.subject_id}:{ordinal}"
                    or not isinstance(agent_id, str) or not agent_id or agent_id in seen
                    or not isinstance(turn_id, str) or not turn_id):
                raise SourceUnavailable("dispatch_history_handoff_identity_invalid")
            agent = runtime.uow.read_agent_binding(agent_id)
            if (agent is None or agent.owner_scope != runtime.owner_scope
                    or agent_id_for(runtime.owner_scope, agent.creation_key) != agent_id):
                raise SourceUnavailable("dispatch_history_agent_binding_missing")
            if ordinal > 1:
                prior = handoffs[ordinal - 2][3]
                if agent.creation_key != prior.get("next_creation_key",
                        f"{intent.subject_id}:rehandoff:{ordinal - 1}"):
                    raise SourceUnavailable("dispatch_history_creation_chain_changed")
            # This is the exact key used by the original event producer, with
            # the first creation retaining its historical subject-based key.
            event_key = intent.subject_id if ordinal == 1 else agent.creation_key
            created = [item for item in events if item[1] == "AgentCreated"
                       and item[2] == "AgentCreated:" + event_key]
            submitted = [item for item in events if item[1] == "InputSubmitted"
                         and item[2] == "InputSubmitted:" + event_key]
            if (len(created) != 1 or len(submitted) != 1
                    or not previous_seq < created[0][0] < submitted[0][0] < seq
                    or created[0][3].get("agent_id") != agent_id
                    or created[0][3].get("expected_turn_id") != turn_id
                    or created[0][3].get("kind") != intent.kind
                    or submitted[0][3].get("kind") != intent.kind):
                raise SourceUnavailable("dispatch_history_original_events_missing")
            receipt = submitted[0][3].get("receipt")
            if not isinstance(receipt, dict) or receipt.get("turn_id") != turn_id:
                raise SourceUnavailable("dispatch_history_submit_receipt_invalid")
            # New producers include these facts explicitly. Older events are
            # bound via the original Agent/Turn bytes below and never upgraded
            # into a fictitious new-format receipt.
            expected = {"previous_creation_key": agent.creation_key, "input_id": intent.input_id,
                        "input_hash": intent.input_hash, "config_hash": sha256_hex(intent.config)}
            if any(name in payload and payload[name] != value for name, value in expected.items()):
                raise SourceUnavailable("dispatch_history_frozen_request_changed")
            other = db.execute(
                "SELECT 1 FROM dispatch_intents WHERE agent_id=? AND intent_id<>? LIMIT 1",
                (agent_id, intent.intent_id),
            ).fetchone()
            if other is not None:
                raise SourceUnavailable("dispatch_history_agent_reused")
            result.append(replace(intent, creation_key=agent.creation_key, agent_id=agent_id,
                                  expected_turn_id=turn_id, receipt=receipt, state="SUBMITTED"))
            seen.add(agent_id)
            previous_seq = seq
        expected_agent = agent_id_for(runtime.owner_scope, intent.creation_key)
        if intent.agent_id not in (None, expected_agent) or expected_agent in seen:
            raise SourceUnavailable("dispatch_history_current_agent_invalid")
        if handoffs:
            # The original writer persists the new key and clears Agent/Turn in
            # the same transaction as its handoff event. Check that linkage.
            last = handoffs[-1][3]
            new_key = last.get("next_creation_key", f"{intent.subject_id}:rehandoff:{len(handoffs)}")
            if new_key != intent.creation_key:
                raise SourceUnavailable("dispatch_history_current_creation_changed")
        return (*result, intent)
