# SPDX-License-Identifier: Apache-2.0
"""Read complete original SDK execution history without executing or settling it.

The Store snapshot owns all local account reads. Each SDK inventory is read in
one read-only transaction, synchronously in the original Store -> SDK order.
Unknown physical outcomes remain explicit facts and keep settlement false.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from simple_harness import RunId
from simple_harness.agents import AgentConfig
from simple_harness.agents.base import BaseAgent, input_hash_for
from simple_harness.agents.contracts import _message_from_json
from simple_harness.agents.runtime import agent_id_for
from simple_harness.contracts import canonical_json
from simple_harness.execution.effects import EffectState, effect_request_hash

from ..contracts.models import jsonable, sha256_hex
from ..governance.budgets import UsageFact
from ..governance.provider_prices import ProviderPrice
from ..graph.execution_contracts import CompleteRead
from ..runtime.dispatch_history import read_dispatch_history
from ..runtime.planning_operations import SourceUnavailable
from ..runtime.taskgraph_local_work import read_local_work
from ..storage.store import DispatchIntent, Store
from .accounting_recovery import _account_subject, _facts, _task_scope
from .taskgraph_execution_sources import _document
from .taskgraph_plan_sources import ImportedExecutionSource


@dataclass(frozen=True, slots=True)
class RuntimeSubjectFacts:
    document: dict[str, Any]
    usage: tuple[UsageFact, ...]
    accounting_complete: bool
    physical_settled: bool
    task_id: str | None


class TaskGraphRuntimeImports:
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator, self.store = orchestrator, orchestrator.store

    @staticmethod
    def _unguarded_usage(bridge: Any, originals: Any, turn: Any) -> tuple[list[UsageFact], bool]:
        """Read legacy SDK calls that never had orchestrator admission grants.

        Identity/account ownership is checked by the caller. Missing usage or
        pricing keeps the original reservation open; it is never a zero charge.
        Guarded calls continue through the original grant validator.
        """
        complete = str(turn.phase) in {"committed", "failed"}
        facts = []
        for original in originals:
            record = bridge.runtime.uow.read_effective_provider_invocation(original.invocation_id)
            if record is None or record.run_id != original.run_id:
                raise SourceUnavailable("taskgraph_runtime_provider_inventory_invalid")
            if original.handoff_attempt == 0:
                continue
            usage = record.usage_json
            tokens = usage.get("usage") if isinstance(usage, Mapping) else None
            if (str(record.state) not in {"succeeded", "failed"}
                    or not isinstance(tokens, Mapping)
                    or any(type(tokens.get(key)) is not int or tokens[key] < 0
                           for key in ("input_tokens", "output_tokens"))):
                complete = False
                continue
            input_tokens, output_tokens = tokens["input_tokens"], tokens["output_tokens"]
            amount = None
            if not bridge.unpriced:
                price = ProviderPrice.from_record(original)
                amount = None if price is None else price.known_charge(
                    record, input_tokens=input_tokens, output_tokens=output_tokens)
                if amount is None:
                    complete = False
                    continue
            facts.append(UsageFact("provider-invocation:" + record.invocation_id,
                                   input_tokens, output_tokens, amount))
        return facts, complete

    def _read(self, intent: DispatchIntent, bridge: Any) -> RuntimeSubjectFacts:
        runtime, store = bridge.runtime, self.store
        executors = read_dispatch_history(store, runtime, intent)
        grants = store.connection.execute(
            "SELECT * FROM provider_token_grants WHERE subject_id=? ORDER BY invocation_id,handoff_ordinal",
            (intent.subject_id,),
        ).fetchall()
        reservation = self.orchestrator.commit.ledger.reservation(intent.subject_id)
        if reservation is None or reservation["mission_id"] != intent.mission_id:
            raise SourceUnavailable("taskgraph_runtime_reservation_missing")
        inventories: list[dict[str, Any]] = []
        facts: list[UsageFact] = []
        grant_ids: set[tuple[str, int]] = set()
        task_id = _task_scope(store, intent)
        account_subject = _account_subject(self.orchestrator.commit, intent, task_id)
        if reservation["account_id"] != "budget:" + account_subject:
            raise SourceUnavailable("taskgraph_runtime_account_owner_mismatch")
        complete, physical = True, True
        for index, executor in enumerate(executors):
            expected_agent = agent_id_for(runtime.owner_scope, executor.creation_key)
            agent = runtime.uow.read_agent_binding(expected_agent)
            own_grants = [row for row in grants if row["agent_id"] == expected_agent]
            if any(row["intent_id"] != intent.intent_id or row["mission_id"] != intent.mission_id
                   for row in own_grants):
                raise SourceUnavailable("taskgraph_runtime_grant_owner_mismatch")
            grant_ids.update((row["invocation_id"], row["handoff_ordinal"]) for row in own_grants)
            entry: dict[str, Any] = {"creation_key": executor.creation_key,
                "agent_id": expected_agent, "historical": index < len(executors) - 1}
            inventories.append(entry)
            if agent is None:
                if (executor.agent_id is not None or executor.expected_turn_id is not None
                        or executor.receipt is not None or own_grants or entry["historical"]):
                    raise SourceUnavailable("taskgraph_runtime_agent_absent_with_handoff")
                entry["materialization"] = "AGENT_ABSENT"
                continue
            if (agent.owner_scope != runtime.owner_scope or agent.creation_key != executor.creation_key
                    or canonical_json(jsonable(agent.config_json)) != canonical_json(
                        AgentConfig.from_json(dict(executor.config["agent_config"])).to_json())):
                raise SourceUnavailable("taskgraph_runtime_config_mismatch")
            turns = runtime.uow.list_agent_turns(expected_agent)
            effects = runtime.uow.list_effects_for_run(agent.run_id)
            originals = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            entry.update(run_id=agent.run_id, binding_hash=sha256_hex(_document(agent)))
            if not turns:
                if (executor.state == "SUBMITTED" or executor.receipt is not None
                        or effects or originals or own_grants or entry["historical"]):
                    raise SourceUnavailable("taskgraph_runtime_unsubmitted_inventory_nonempty")
                entry["materialization"] = "TURN_ABSENT"
                continue
            expected_turn = BaseAgent(runtime, agent).turn_id_for(executor.input_id)
            if len(turns) != 1 or turns[0].turn_id != expected_turn:
                raise SourceUnavailable("taskgraph_runtime_turn_inventory_invalid")
            turn = turns[0]
            message = _message_from_json(dict(executor.config["message"]))
            if (turn.agent_id != expected_agent or turn.input_id != executor.input_id
                    or sha256_hex(executor.config["message"]) != executor.input_hash
                    or turn.input_hash != input_hash_for(message)
                    or canonical_json(jsonable(turn.input_json)) != canonical_json({"message": message.to_dict()})):
                raise SourceUnavailable("taskgraph_runtime_turn_input_changed")
            entry.update(materialization="TURN_PRESENT", turn_id=turn.turn_id,
                         turn_hash=sha256_hex(_document(turn)), phase=str(turn.phase))
            bound = executor.agent_id == expected_agent and executor.expected_turn_id == expected_turn
            if not bound:
                # A crash may occur after SDK submit and before saving its receipt.
                # The inventory is visible, but original recovery must bind it
                # before accounting or convergence may declare completion.
                complete = physical = False
            else:
                duplicates = store.connection.execute(
                    "SELECT COUNT(*) FROM dispatch_intents WHERE agent_id=?", (expected_agent,),
                ).fetchone()[0]
                if duplicates != (0 if entry["historical"] else 1):
                    raise SourceUnavailable("taskgraph_runtime_agent_reused")
                if (executor.config.get("provider_admission_fingerprint") or own_grants
                        or runtime.ports.provider_admission is not None):
                    actual, settled, scope = _facts(self.orchestrator.commit, bridge,
                        executor, reservation, grants=own_grants, historical=entry["historical"])
                    if task_id is not None and scope != task_id:
                        raise SourceUnavailable("taskgraph_runtime_account_scope_changed")
                    task_id = scope
                else:
                    actual, settled = self._unguarded_usage(bridge, originals, turn)
                facts.extend(actual)
                complete = complete and settled
                physical = physical and settled
            provider_facts = []
            for original in originals:
                effective = runtime.uow.read_effective_provider_invocation(original.invocation_id)
                if effective is None or original.run_id.value != agent.run_id:
                    raise SourceUnavailable("taskgraph_runtime_provider_inventory_invalid")
                provider_facts.append({"invocation_id": original.invocation_id,
                    "original_hash": sha256_hex(_document(original)),
                    "effective_hash": sha256_hex(_document(effective)), "state": str(effective.state)})
            effect_facts = []
            for effect in effects:
                if (effect.run_id.value != agent.run_id or effect.request_hash != effect_request_hash(
                        tool_name=effect.tool_name, arguments=jsonable(effect.arguments))):
                    raise SourceUnavailable("taskgraph_runtime_effect_identity_mismatch")
                effect_facts.append({"effect_id": effect.effect_id.value,
                    "record_hash": sha256_hex(_document(effect)), "state": str(effect.state)})
                if effect.state not in {EffectState.SUCCEEDED, EffectState.FAILED, EffectState.REJECTED}:
                    physical = False
            entry.update(provider_invocations=provider_facts, effects=effect_facts)
        if grant_ids != {(row["invocation_id"], row["handoff_ordinal"]) for row in grants}:
            raise SourceUnavailable("taskgraph_runtime_grant_outside_executor_history")
        if len({fact.usage_ref for fact in facts}) != len(facts):
            raise SourceUnavailable("taskgraph_runtime_invocation_reused")
        if self.orchestrator.commit.ledger.has_unknown_usage(intent.subject_id):
            physical = False
        imports = []
        for fact in facts:
            imported = store.connection.execute(
                "SELECT subject_id,mission_id,input_tokens,output_tokens,cost_micros,unknown "
                "FROM imported_usage WHERE usage_ref=?", (fact.usage_ref,),
            ).fetchone()
            if imported is None:
                physical = False
            elif tuple(imported) != (intent.subject_id, intent.mission_id, fact.input_tokens,
                                     fact.output_tokens, fact.cost_micros, int(fact.unknown)):
                # A still-unknown old import awaits the real accounting importer;
                # differing final amounts or identities are corruption.
                if (imported[0] != intent.subject_id or imported[1] != intent.mission_id or not imported[5]):
                    raise SourceUnavailable("taskgraph_runtime_imported_usage_mismatch")
                physical = False
            imports.append({"usage_ref": fact.usage_ref, "actual": _document(fact),
                            "imported": None if imported is None else list(imported)})
        return RuntimeSubjectFacts({"intent_id": intent.intent_id, "subject_id": intent.subject_id,
            "intent_hash": sha256_hex(intent.to_json()), "executors": inventories,
            "provider_grants": [dict(row) for row in grants], "usage": imports,
            "accounting_complete": complete, "physical_settled": physical},
            tuple(facts), complete, physical, task_id)

    def read_subject(self, intent: DispatchIntent) -> RuntimeSubjectFacts:
        with self.store.read_view():
            bridge = self.orchestrator.bridge_for(intent)
            with bridge.runtime.uow.database.transaction(read_only=True):
                return self._read(intent, bridge)

    def __call__(self, store: Store, mission_id: str) -> CompleteRead[ImportedExecutionSource]:
        if store is not self.store:
            raise SourceUnavailable("taskgraph_runtime_store_mismatch")
        with store.read_view(), ExitStack() as stack:
            local = read_local_work(store, mission_id)
            intents, bridges, databases = [], {}, set()
            for row in local.to_json()["intents"]:
                intent = store.get_intent(row["intent_id"])
                if intent is None:
                    raise SourceUnavailable("taskgraph_runtime_intent_missing")
                bridge = self.orchestrator.bridge_for(intent)
                db = bridge.runtime.uow.database
                if id(db) not in databases:
                    stack.enter_context(db.transaction(read_only=True))
                    databases.add(id(db))
                intents.append(intent)
                bridges[intent.intent_id] = bridge
            subjects = [self._read(intent, bridges[intent.intent_id]).document for intent in intents]
            body: dict[str, Any] = {"mission_id": mission_id,
                                   "local_work_digest": local.digest, "subjects": subjects}
            value = ImportedExecutionSource(mission_id=mission_id, kind="runtime",
                canonical_document=canonical_json(body))
            return CompleteRead(value=value, source_id=mission_id+":original-runtime-history",
                source_digest=sha256_hex(value.to_json()), through_seq=store.last_event_seq(mission_id))
