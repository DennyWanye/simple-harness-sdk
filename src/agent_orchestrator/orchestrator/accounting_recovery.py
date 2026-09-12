"""Import durable SDK accounting into original reservations, without execution.

This path consumes existing accounting evidence only. SDK reconciliation may
produce that evidence elsewhere; no provider observation/dispatch occurs here.
Terminal business records are deliberately not an admission prerequisite.
"""

from collections.abc import Mapping

from simple_harness import RunId
from simple_harness.agents import AgentConfig
from simple_harness.agents.base import input_hash_for
from simple_harness.agents.contracts import _message_from_json
from simple_harness.contracts import canonical_json
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.execution.uow import UnitOfWorkConflict

from ..contracts.models import jsonable, sha256_hex
from ..governance.budgets import BudgetError, UsageFact
from ..governance.provider_prices import ProviderPrice


def _require(condition, message):
    if not condition:
        raise BudgetError("late accounting binding: " + message)


def _facts(commit, bridge, intent, reservation):
    """Validate original subject -> single Agent/Turn -> every physical invocation.

    A terminal grant alone is insufficient. Even SETTLED grants are re-bound to
    the immutable SDK record, and only effective actual usage becomes a fact.
    """
    store, uow = commit.store, bridge.runtime.uow
    binding = uow.read_agent_binding(intent.agent_id)
    turn = uow.read_agent_turn(intent.expected_turn_id)
    if binding is None or turn is None:
        return [], False, None
    _require(reservation["mission_id"] == intent.mission_id, "reservation Mission")
    _require(store.get_mission(intent.mission_id) is not None, "missing Mission")
    _require(binding.agent_id == intent.agent_id and turn.agent_id == binding.agent_id,
             "Agent/Turn")
    _require(binding.creation_key == intent.creation_key, "creation key")
    _require(turn.input_id == intent.input_id, "input id")
    _require(len(uow.list_agent_turns(intent.agent_id)) == 1, "Agent reused across turns")
    duplicates = store.connection.execute(
        "SELECT COUNT(*) FROM dispatch_intents WHERE agent_id=?", (intent.agent_id,),
    ).fetchone()[0]
    _require(duplicates == 1, "Agent reused across subjects")
    config = AgentConfig.from_json(dict(intent.config["agent_config"])).to_json()
    # SDK records recursively freeze JSON as mappingproxy/tuple. Compare the
    # detached JSON representation, never weaken canonical_json's public schema
    # or mutate the original record merely to make it serializable.
    _require(canonical_json(config) == canonical_json(jsonable(binding.config_json)),
             "Agent config")
    message = intent.config["message"]
    _require(sha256_hex(message) == intent.input_hash, "frozen input hash")
    actual_message = _message_from_json(dict(message))
    _require(turn.input_hash == input_hash_for(actual_message), "SDK input hash")
    _require(canonical_json(jsonable(turn.input_json)) == canonical_json(
        {"message": actual_message.to_dict()}), "SDK input bytes")
    task_id = intent.config.get("task_id")
    if intent.kind == "attempt":
        attempt = store.get_attempt(intent.subject_id)
        _require(attempt is not None and attempt.mission_id == intent.mission_id, "Attempt")
        task_id = attempt.task_id
    if task_id is not None:
        task = store.get_task(task_id)
        _require(task is not None and task.mission_id == intent.mission_id, "Task")
    _require(reservation["account_id"] == "budget:" + (task_id or intent.mission_id),
             "original account")
    terminal = str(turn.phase) in {"committed", "failed"}
    complete = terminal
    facts = []
    originals = uow.list_provider_invocations(RunId(binding.run_id))
    grants = store.connection.execute(
        "SELECT * FROM provider_token_grants WHERE subject_id=?", (intent.subject_id,),
    ).fetchall()
    ids = {original.invocation_id for original in originals}
    _require(all(row["invocation_id"] in ids for row in grants), "grant absent from SDK Run")
    for original in originals:
        if original.handoff_attempt == 0:
            continue
        rows = [row for row in grants if row["invocation_id"] == original.invocation_id
                and row["handoff_ordinal"] == original.handoff_attempt]
        _require(len(rows) == 1, "no unique physical grant")
        row = rows[0]
        _require(
            row["intent_id"] == intent.intent_id and row["mission_id"] == intent.mission_id
            and row["agent_id"] == binding.agent_id and row["turn_id"] == turn.turn_id
            and row["fingerprint"] == intent.config.get("provider_admission_fingerprint")
            and row["request_hash"] == original.request_fingerprint
            and original.run_id.value == binding.run_id,
            "physical identity",
        )
        price = ProviderPrice.from_record(original)
        legacy_unpriced = (row["price_digest"] is None and row["price_json"] is None
                           and row["cost_upper_micros"] is None and (price is None or
                           price.digest == FrozenPriceEstimator(
                               "consumer-v1", "consumer", 0, 0).snapshot_digest))
        _require(legacy_unpriced or (
            row["price_digest"] == (None if price is None else price.digest)
            and row["price_json"] == (None if price is None else price.json)), "original price")
        record = uow.read_effective_provider_invocation(original.invocation_id)
        _require(record is not None, "missing effective invocation")
        usage = record.usage_json
        usage = usage.get("usage") if isinstance(usage, Mapping) else None
        if str(record.state) not in {"succeeded", "failed"} or not isinstance(usage, Mapping):
            complete = False
            continue
        values = [usage.get("input_tokens"), usage.get("output_tokens")]
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
            complete = False
            continue
        input_tokens, output_tokens = values
        amount = None
        if row["cost_upper_micros"] is not None:
            amount = None if price is None else price.known_charge(
                record, input_tokens=input_tokens, output_tokens=output_tokens)
            if amount is None:
                complete = False
                continue
        facts.append(UsageFact("provider-invocation:" + record.invocation_id,
                               input_tokens, output_tokens, amount))
    return facts, complete, task_id


def import_late_accounting(orch) -> bool:
    """Scan open original holds across all Mission and service lifecycle states.

    Orch -> SDK lock order. Repeated calls are no-ops once the original subject
    settles. Unknown/unavailable/foreign evidence never releases its reservation.
    """
    store = orch.store
    if not store.has_table("provider_token_grants"):
        return False
    # Guard recovery reads effective receipts and validates original price. An
    # actual overrun is committed before it raises; its real cost must still be
    # imported. Other failures stay fail-closed and are checked per subject below.
    for pool in orch.assembled.pools.values():
        guard = pool.bridge.runtime.ports.provider_admission
        if guard is not None:
            try:
                guard.recover(pool.bridge.runtime.uow)
            except (ProviderAdmissionDenied, UnitOfWorkConflict) as error:
                orch._note(f"accounting grant recovery: {error}")
    rows = store.connection.execute(
        "SELECT i.intent_id FROM dispatch_intents i JOIN budget_reservations r"
        " ON r.subject_id=i.subject_id WHERE r.state='RESERVED'"
        " AND i.state IN ('SETTLED','FAILED') AND i.agent_id IS NOT NULL"
        " AND i.expected_turn_id IS NOT NULL ORDER BY i.created_at",
    ).fetchall()
    progressed = False
    for row in rows:
        try:
            with store.transaction():
                intent = store.get_intent(row[0])
                reservation = orch.commit.ledger.reservation(intent.subject_id)
                if reservation is None or reservation["state"] == "SETTLED":
                    continue
                # Old unguarded intents retain their historical recovery path.
                if not intent.config.get("provider_admission_fingerprint"):
                    continue
                bridge = orch.bridge_for(intent)  # exact persisted pool; never fallback
                facts, complete, task_id = _facts(orch.commit, bridge, intent, reservation)
                imported = orch.commit.import_usage(intent.subject_id, intent.mission_id, facts)
                settled = complete and not orch.commit.ledger.has_unknown_usage(
                    intent.subject_id)
                if settled:
                    orch.commit._settle_subject(
                        intent.subject_id, intent.mission_id, task_id=task_id)
                progressed = progressed or bool(imported) or settled
        except (BudgetError, ValueError, KeyError, TypeError, UnitOfWorkConflict) as error:
            orch._note(f"accounting subject {row[0]} held: {error}")
    return progressed
