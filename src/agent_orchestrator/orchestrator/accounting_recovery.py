"""Import durable SDK accounting into original reservations, without execution.

This path consumes existing accounting evidence only. SDK reconciliation may
produce that evidence elsewhere; no provider observation/dispatch occurs here.
Terminal business records are deliberately not an admission prerequisite.
"""

import time
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
from ..contracts.state_machines import TERMINAL_MISSION
from ..runtime.planning_operations import SourceUnavailable
from ..runtime.provider_budget_guard import ProviderBudgetGuard

# 已结束任务的旧预留/旧授权多久重核一次（秒）。
ENDED_MISSION_RECHECK_SECONDS = 300.0


def _require(condition, message):
    if not condition:
        raise BudgetError("late accounting binding: " + message)


def _task_scope(store, intent):
    """Resolve the original task link, including Critic's frozen Attempt view."""
    task_id = intent.config.get("task_id")
    attempt = None
    if intent.kind == "attempt":
        attempt = store.get_attempt(intent.subject_id)
        _require(attempt is not None, "Attempt")
    elif intent.kind == "critic" and intent.config.get("attempt_id"):
        # Mission critics also use view ids here; only a real original Attempt
        # provides task ownership. Never parse a subject/view id for a Task.
        attempt = store.get_attempt(intent.config["attempt_id"])
    if attempt is not None:
        _require(attempt.mission_id == intent.mission_id, "Attempt Mission")
        _require(task_id is None or task_id == attempt.task_id, "Attempt Task")
        task_id = attempt.task_id
    if task_id is not None:
        task = store.get_task(task_id)
        _require(task is not None and task.mission_id == intent.mission_id, "Task")
    return task_id


def _account_subject(commit, intent, task_id):
    if intent.config.get("assurance_protocol") == "assurance-exec-v1.1":
        from ..contracts.resolution import ReviewAccount
        from ..storage.assurance_reads import AssuranceReader
        from ..storage.htn_store import HtnStore
        from .assurance_review_transport import read_review_invocation_locked
        mission = commit.store.get_mission(intent.mission_id)
        _require(mission is not None, "missing Mission")
        _, binding = read_review_invocation_locked(
            commit, AssuranceReader(commit.store, tenant_id=mission.tenant_id,
                                    mission_id=mission.id), intent.intent_id)
        package = HtnStore(commit.store).get_review_package(binding.to_json()["package_ref"]["id"])
        if package.account in {ReviewAccount.MISSION, ReviewAccount.MISSION_PLANNING}:
            return mission.id
        _require(task_id == binding.to_json()["subject"]["owner_task_ref"]["id"], "review account owner")
        if package.account is ReviewAccount.OPERATION_TASK:
            # Same rule as the invocation writer: the operation's producer Task.
            from .assurance_review_transport import operation_task_id
            return operation_task_id(commit.store, mission.id, package)
        return task_id
    return intent.mission_id if intent.kind == "manager" else (task_id or intent.mission_id)


def _facts(commit, bridge, intent, reservation, *, grants=None, historical=False):
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
    if historical:
        _require(duplicates == 0, "historical Agent reused across subjects")
    else:
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
    task_id = _task_scope(store, intent)
    # Manager is funded by the Mission; task_id names its historical evidence
    # scope, not the account charged by create_service_intent.
    account_subject = _account_subject(commit, intent, task_id)
    _require(reservation["account_id"] == "budget:" + account_subject, "original account")
    terminal = str(turn.phase) in {"committed", "failed"}
    complete = terminal
    facts = []
    originals = uow.list_provider_invocations(RunId(binding.run_id))
    if grants is None:
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
    # 2026-09-28 真机（第四轮）：运行中每轮都有写入，下面的"安静代次"永远不成立，已结束
    # 任务留下的几十个"用量未知"旧预留与旧授权每轮仍被完整重核一遍。它们不会再有新调用，
    # 只可能等到迟到的对账记录：每 5 分钟重核一次；未结束任务的照旧每轮都核。
    now = time.monotonic()
    last_full = getattr(orch, "_late_accounting_ended_at", None)
    full = last_full is None or now - last_full >= ENDED_MISSION_RECHECK_SECONDS
    live = None if full else _live_missions(store)
    if full:
        orch._late_accounting_ended_at = now
    # Guard recovery reads effective receipts and validates original price. An
    # actual overrun is committed before it raises; its real cost must still be
    # imported. Other failures stay fail-closed and are checked per subject below.
    for pool in orch.assembled.pools.values():
        guard = pool.bridge.runtime.ports.provider_admission
        if guard is not None:
            try:
                if live is not None and isinstance(guard, ProviderBudgetGuard):
                    guard.recover(pool.bridge.runtime.uow, missions=live)
                else:
                    guard.recover(pool.bridge.runtime.uow)
            except (ProviderAdmissionDenied, UnitOfWorkConflict) as error:
                orch._note(f"accounting grant recovery: {error}")
    # 2026-09-28 真机：几十个"用量未知、按上限预留"的旧调用每轮都被完整重读一遍（约三成
    # CPU），而它们的事实只来自编排库与各执行池库。两边都没有任何写入时结果必然相同，跳过；
    # 本轮自己写过库（导入了用量）就不记代次，下一轮照常重读。只核了未结束任务的一轮不能
    # 证明已结束任务的结果不变，所以"全量安静"单独记。
    generation = _holds_generation(orch)
    marker = "_late_accounting_full_quiet" if full else "_late_accounting_quiet"
    quiet = generation is not None and generation == getattr(orch, marker, None)
    rows = () if quiet else _open_holds(store)
    if live is not None:
        rows = [row for row in rows if row[1] in live]
    progressed = False
    for row in rows:
        progressed = _import_hold(orch, row[0]) or progressed
    after = _holds_generation(orch)
    unchanged = after if after is not None and after == generation else None
    orch._late_accounting_quiet = unchanged
    if full:
        orch._late_accounting_full_quiet = unchanged
    from .taskgraph_action_settlement import settle_resolved_actions
    return settle_resolved_actions(orch) or progressed


def _import_hold(orch, intent_id: str) -> bool:
    store = orch.store
    try:
        with store.transaction():
            intent = store.get_intent(intent_id)
            reservation = orch.commit.ledger.reservation(intent.subject_id)
            if reservation is None or reservation["state"] == "SETTLED":
                return False
            from .taskgraph_dispatch import taskgraph_enabled
            graph_enabled = taskgraph_enabled(store, intent.mission_id)
            from ..storage.assurance_store import AssuranceStore
            assured = AssuranceStore(store).lane(intent.mission_id) == "ASSURANCE_1_1"
            # TaskGraph can also prove a request never materialized. Legacy
            # unguarded intents retain their historical recovery path.
            if not graph_enabled and not assured and not intent.config.get("provider_admission_fingerprint"):
                return False
            bridge = orch.bridge_for(intent)  # exact persisted pool; never fallback
            if graph_enabled or assured:
                from .taskgraph_runtime_imports import TaskGraphRuntimeImports
                source = TaskGraphRuntimeImports(orch).read_subject(intent)
                facts, complete, task_id = source.usage, source.accounting_complete, source.task_id
            else:
                if intent.agent_id is None or intent.expected_turn_id is None:
                    return False
                facts, complete, task_id = _facts(orch.commit, bridge, intent, reservation)
            imported = orch.commit.import_usage(intent.subject_id, intent.mission_id, facts)
            settled = (complete and (not (graph_enabled or assured) or source.physical_settled)
                       and not orch.commit.ledger.has_unknown_usage(intent.subject_id))
            if settled:
                orch.commit._settle_subject(
                    intent.subject_id, intent.mission_id, task_id=task_id)
            return bool(imported) or settled
    except (BudgetError, ValueError, KeyError, TypeError, UnitOfWorkConflict, SourceUnavailable) as error:
        orch._note(f"accounting subject {intent_id} held: {error}")
        return False


def _open_holds(store) -> list:
    return store.connection.execute(
        "SELECT i.intent_id, i.mission_id FROM dispatch_intents i JOIN budget_reservations r"
        " ON r.subject_id=i.subject_id WHERE r.state='RESERVED'"
        " AND i.state IN ('SETTLED','FAILED') ORDER BY i.created_at",
    ).fetchall()


def _live_missions(store) -> frozenset[str]:
    ended = sorted(str(status) for status in TERMINAL_MISSION)
    return frozenset(row[0] for row in store.connection.execute(
        f"SELECT mission_id FROM missions WHERE status NOT IN ({','.join('?' * len(ended))})",
        ended,
    ).fetchall())


def _connection_generation(connection) -> tuple[int, int, int] | None:
    if connection.in_transaction:  # uncommitted rows may still roll back
        return None
    version = int(connection.execute("PRAGMA data_version").fetchone()[0])
    return (id(connection), int(connection.total_changes), version)


def _holds_generation(orch) -> tuple | None:
    """Changes whenever anything the open-hold facts are read from may have changed."""

    parts: list = [orch.store.read_generation()]
    for key in sorted(orch.assembled.pools):
        parts.append(_connection_generation(orch.assembled.pools[key].bridge.runtime.uow.database.connection))
    return None if any(part is None for part in parts) else tuple(parts)
