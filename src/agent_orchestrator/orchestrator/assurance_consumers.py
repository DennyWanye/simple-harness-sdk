# SPDX-License-Identifier: Apache-2.0
"""VALIDITY / CLOSEOUT / NOTIFY consumers on the original Assurance tick.

Each adapter classifies original durable Events into stable work keys and
prepares a bounded, transaction-free computation whose ``commit`` re-reads the
same facts under the final lock and writes one original receipt. None of them
runs a model, opens a pool, releases a hold or changes a Mission status: the
unique final writer (spec §7.1, handoff item 7) is installed separately and
consumes a READY closeout; NOTIFY only transports ``{mission_id, event_id,
state_version}`` (spec §7.3), never a report body.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..assurance.codec import AssuranceError, canonical, decode, fields, fingerprint, integer, text
from ..assurance.expiry import EXPIRY_EVENT, classify_expiry, validity_work_key
from ..assurance.refs import AssuranceRef, Pin
from ..contracts import Event
from ..storage.assurance_reads import read_epochs_locked
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import WorkClaim, WorkTarget, atomic
from ..storage.htn_store import HtnStore
from ..storage.store import _event_from_row
from .assurance_final_writer import recorded_judgment
from .assurance_tick import AssuranceWait, PreparedAssuranceWork

NOTIFICATION_EVENT = "AssuranceStatusNotificationRequested"
VALIDITY_CHECKED_EVENT = "AssuranceUseValidityChecked"
CLOSEOUT_EVENT = "AssuranceCloseoutEvaluated"
UPPER_BOUND_EVENT = "ReservationCountedAtUpperBound"  # 2026-09-26 user decision
NOTIFIED_EVENT = "AssuranceStatusNotified"
ACTIVATION_EVENT = "AssuranceProfileActivated"

# Events whose appearance can change the closeout answer (event-consumer-map
# classes SOURCE_OR_AUTHORITY_CHANGED / BUSINESS_OR_RUNTIME_SETTLED). Listing
# them here is the AS-0 adapter registration; nothing is inferred from a prefix.
CLOSEOUT_SOURCE_EVENTS = frozenset(
    {
        "AssuranceEvidenceChanged",
        "GoalResolutionCommitted",
        "AcceptanceCommitted",
        "OperationOutcomeAccepted",
        "DeliveryReceiptRecorded",
        "IntentSettled",
        "BudgetReleased",
        "AssuranceUseCertified",
        "AssuranceLocalCheckImported",
        "AssuranceExecutorCheckImported",
        "MissionCriteriaJudged",
        "MissionSuccessJudged",
        "AssuranceCloseoutRequested",
        "MissionCompleted",
        "MissionFailed",
        "MissionCancelled",
        "ActionFailed",
        "ActionCancelled",
        "AttemptCancelled",
        "TaskCompleted",
        "TaskFailed",
        "TaskCancelled",
    }
)
VALIDITY_SOURCE_EVENTS = frozenset(
    {"AssuranceEvidenceChanged", "AssuranceLocalCheckImported", "AssuranceExecutorCheckImported"}
)
# Own outputs: DIAGNOSTIC_ONLY for every consumer, never re-ingested as work.
DIAGNOSTIC_EVENTS = frozenset({VALIDITY_CHECKED_EVENT, CLOSEOUT_EVENT, NOTIFIED_EVENT})

MAX_WAKE_ROWS = 256


def _receipt_ref(
    store: Any, receipt_id: str, kind: str, subject_id: str, body: dict
) -> AssuranceRef:
    digest = fingerprint(body)
    existing = store.get_receipt(receipt_id)
    if existing is not None:
        if dict(existing) != body:
            raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", receipt_id)
        return AssuranceRef("commit_receipt", Pin(receipt_id, 0, digest))
    store.insert_receipt(
        commit_id=receipt_id,
        kind=kind,
        subject_id=subject_id,
        base_version=0,
        proposal_hash=digest,
        receipt=body,
    )
    return AssuranceRef("commit_receipt", Pin(receipt_id, 0, digest))


def _trigger(store: Any, claim: WorkClaim) -> Event:
    row = store.connection.execute(
        "SELECT * FROM events WHERE event_id=? AND mission_id=?",
        (claim.trigger_event_id, claim.mission_id),
    ).fetchone()
    if row is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "work trigger event")
    event = _event_from_row(row)
    if event.seq != claim.target_epoch:
        raise AssuranceError("WORK_TRIGGER_IDENTITY")
    return event


def _trigger_json(event: Event) -> dict:
    return {"event_id": event.id, "seq": event.seq, "hash": fingerprint(event.to_json())}


class _ConsumerBase:
    def __init__(self, commit: Any, *, tenant_id: str, consumer: str) -> None:
        self.commit = commit
        self.store = commit.store
        self.tenant_id = text(tenant_id)
        self.consumer = consumer

    def _root(self) -> str:
        gate = self.commit._assurance_root_gate
        if gate is None:
            raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
        return gate.require_execution().root_incarnation_id

    def _require_mission(self, mission_id: str) -> Any:
        mission = self.store.get_mission(mission_id)
        if mission is None or mission.tenant_id != self.tenant_id:
            raise AssuranceError("ASSURANCE_TENANT_MISMATCH")
        if AssuranceStore(self.store).lane(mission_id) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
        return mission

    def _check_claim(self, claim: WorkClaim, event: Event, *, exact: bool = True) -> None:
        if claim.consumer != self.consumer:
            raise AssuranceError(self.consumer + "_WORK_IDENTITY")
        targets = self.classify(event)
        if exact:
            found = WorkTarget(claim.work_key, claim.target_fingerprint) in targets
        else:
            found = claim.work_key in {target.work_key for target in targets}
        if not found:
            raise AssuranceError(self.consumer + "_WORK_IDENTITY")

    def classify(self, event: Event) -> tuple[WorkTarget, ...]:  # pragma: no cover - abstract
        raise NotImplementedError


# ------------------------------------------------------------------ VALIDITY
class AssuranceValidityConsumer(_ConsumerBase):
    """Timing owner for issued use certificates (spec §8.4).

    ``AssuranceExpiry.emit_due`` (inside the tick) turns each USABLE certificate
    boundary into one ``AssuranceUseExpiryDue`` event; a source/authority change
    wakes every live certificate consumer of the Mission. The work re-reads the
    exact certificate and records whether it is still CURRENT or STALE (with
    the reasons EXPIRED / SOURCE_CHANGED / ROOT_CHANGED / SUPERSEDED /
    TIME_DISCONTINUITY). STALE is an observation, not a revocation: a consumed
    certificate is history, and any new use recomputes its own candidate. The
    worker never extends a deadline, never re-issues a certificate and never
    repairs a consumer's stored use.
    """

    def __init__(self, commit: Any, *, tenant_id: str) -> None:
        super().__init__(commit, tenant_id=tenant_id, consumer="VALIDITY")

    def _live_certificates(self, mission_id: str) -> list[Any]:
        # Latest inserted certificate per exact consumer identity; only USABLE
        # ones carry a validity to observe.
        return self.store.connection.execute(
            "SELECT c.* FROM assurance_use_certificates c WHERE c.mission_id=? "
            "AND json_extract(c.certificate_json,'$.decision')='USABLE' "
            "AND NOT EXISTS(SELECT 1 FROM assurance_use_certificates newer "
            "WHERE newer.mission_id=c.mission_id AND newer.consumer_kind=c.consumer_kind "
            "AND newer.consumer_id=c.consumer_id AND newer.purpose=c.purpose "
            "AND newer.scope_id=c.scope_id AND newer.rowid>c.rowid) "
            "ORDER BY c.rowid LIMIT ?",
            (mission_id, MAX_WAKE_ROWS + 1),
        ).fetchall()

    def classify(self, event: Event) -> tuple[WorkTarget, ...]:
        if event.type == EXPIRY_EVENT:
            return classify_expiry(event, "VALIDITY")
        if event.type not in VALIDITY_SOURCE_EVENTS:
            return ()
        rows = self._live_certificates(event.mission_id)
        if len(rows) > MAX_WAKE_ROWS:
            raise AssuranceError("VALIDITY_WAKEUP_INVENTORY_INCOMPLETE")
        change = fingerprint(event.to_json())
        targets = {}
        for row in rows:
            key = validity_work_key(decode(row["certificate_json"]))
            targets[key] = WorkTarget(
                key,
                fingerprint(
                    {
                        "certificate_id": row["certificate_id"],
                        "certificate_hash": row["certificate_hash"],
                        "change_event": change,
                    }
                ),
            )
        return tuple(targets[key] for key in sorted(targets))

    def _subject(self, claim: WorkClaim, event: Event) -> Any:
        """The exact certificate row this work observes, from the trigger itself."""
        if event.type == EXPIRY_EVENT:
            body = dict(event.payload)
            row = self.store.connection.execute(
                "SELECT * FROM assurance_use_certificates WHERE mission_id=? AND certificate_id=?",
                (claim.mission_id, text(body.get("certificate_id"))),
            ).fetchone()
            if row is None or row["certificate_hash"] != body.get("certificate_hash"):
                raise AssuranceError("SOURCE_UNAVAILABLE", "expired certificate")
            return row
        for row in self._live_certificates(claim.mission_id):
            if validity_work_key(decode(row["certificate_json"])) == claim.work_key:
                return row
        raise AssuranceError("SOURCE_UNAVAILABLE", "live certificate for work key")

    def _observe_locked(self, row: Any, *, now_ms: int) -> dict:
        certificate = decode(row["certificate_json"])
        if fingerprint(certificate) != row["certificate_hash"]:
            raise AssuranceError("CERTIFICATE_HASH_MISMATCH")
        epochs = read_epochs_locked(self.store.connection, row["mission_id"])
        root = self._root()
        reasons = []
        if certificate["root_incarnation_id"] != root:
            reasons.append("ROOT_CHANGED")
        if (
            certificate["mission_epoch"],
            certificate["environment_epoch"],
            certificate["clock_generation"],
        ) != (epochs.mission, epochs.environment, epochs.clock_generation):
            reasons.append("SOURCE_CHANGED")
        if epochs.clock_state != "STABLE":
            reasons.append("TIME_DISCONTINUITY")
        if row["not_after_ms"] is not None and now_ms >= row["not_after_ms"]:
            reasons.append("EXPIRED")
        superseded = self.store.connection.execute(
            "SELECT 1 FROM assurance_use_certificates newer WHERE newer.mission_id=? "
            "AND newer.consumer_kind=? AND newer.consumer_id=? AND newer.purpose=? "
            "AND newer.scope_id=? AND newer.rowid>(SELECT rowid FROM assurance_use_certificates "
            "WHERE certificate_id=?) LIMIT 1",
            (
                row["mission_id"],
                row["consumer_kind"],
                row["consumer_id"],
                row["purpose"],
                row["scope_id"],
                row["certificate_id"],
            ),
        ).fetchone()
        if superseded is not None:
            reasons.append("SUPERSEDED")
        return {
            "mission_id": row["mission_id"],
            "certificate_id": row["certificate_id"],
            "certificate_hash": row["certificate_hash"],
            "consumer_kind": row["consumer_kind"],
            "consumer_id": row["consumer_id"],
            "purpose": row["purpose"],
            "scope_id": row["scope_id"],
            "not_after_ms": row["not_after_ms"],
            "validity": "CURRENT" if not reasons else "STALE",
            "reasons": reasons,
            "observed_at_ms": now_ms,
            "epochs": epochs.to_json(),
        }

    async def prepare(self, claim: WorkClaim) -> PreparedAssuranceWork | AssuranceWait:
        if self.store.connection.in_transaction:
            raise AssuranceError("ASSURANCE_PREPARATION_INSIDE_TRANSACTION")
        with self.store.read_view():
            self._require_mission(claim.mission_id)
            event = _trigger(self.store, claim)
            self._check_claim(claim, event, exact=event.type == EXPIRY_EVENT)
            row = self._subject(claim, event)
            preview = self._observe_locked(row, now_ms=int(self.store.now * 1000))
        trigger = _trigger_json(event)
        certificate_id, certificate_hash = row["certificate_id"], row["certificate_hash"]

        def commit() -> AssuranceRef:
            with atomic(self.store):
                self._require_mission(claim.mission_id)
                current = self.store.connection.execute(
                    "SELECT * FROM assurance_use_certificates WHERE mission_id=? "
                    "AND certificate_id=?",
                    (claim.mission_id, certificate_id),
                ).fetchone()
                if current is None or current["certificate_hash"] != certificate_hash:
                    raise AssuranceError("RECHECK_REQUIRED")
                observed = self._observe_locked(current, now_ms=int(self.store.now * 1000))
                if (observed["validity"], observed["reasons"]) != (
                    preview["validity"],
                    preview["reasons"],
                ):
                    raise AssuranceError("RECHECK_REQUIRED")
                body = {
                    **observed,
                    "trigger": trigger,
                    "work_key": claim.work_key,
                    "target_epoch": claim.target_epoch,
                }
                receipt_id = "assurance-validity:" + fingerprint(
                    {
                        "certificate_id": certificate_id,
                        "certificate_hash": certificate_hash,
                        "trigger": trigger,
                    }
                )
                ref = _receipt_ref(
                    self.store, receipt_id, VALIDITY_CHECKED_EVENT, certificate_id, body
                )
                self.commit._emit(
                    VALIDITY_CHECKED_EVENT,
                    claim.mission_id,
                    key=receipt_id,
                    payload={
                        key: body[key]
                        for key in (
                            "certificate_id",
                            "certificate_hash",
                            "consumer_kind",
                            "consumer_id",
                            "purpose",
                            "scope_id",
                            "validity",
                            "reasons",
                            "observed_at_ms",
                        )
                    },
                )
                return ref

        return PreparedAssuranceWork(commit)


# ------------------------------------------------------------------ CLOSEOUT
class AssuranceCloseoutConsumer(_ConsumerBase):
    """Derived closeout readiness of an assured Mission (spec §7.2).

    The original missions row stays the final-status authority. This consumer
    only projects, into ``assurance_closeouts``, whether the root business
    requirement is met AND every original responsibility has converged:
    NOT_READY (no adopted root resolution / current evidence invalid / root
    Scope unmet), BLOCKED_UNKNOWN (an approved effect is in
    RECONCILIATION_REQUIRED), DRAINING (open intents, unsettled reservations,
    unknown usage), READY (all converged: also the Mission judge's recorded
    success judgment on an ACTIVE Mission). READY→FINALIZED belongs to the unique
    final writer ``assurance_final_writer.finalize_assured_mission`` (item 7),
    installed as ``finalizer(mission_id, check)`` and called inside the same
    transaction when the re-evaluation lands READY.
    """

    VOLATILE = frozenset({"evaluated_at_ms", "epochs"})

    def __init__(
        self,
        commit: Any,
        *,
        tenant_id: str,
        finalizer: Callable[[str, Mapping[str, Any]], AssuranceRef | None] | None = None,
    ) -> None:
        super().__init__(commit, tenant_id=tenant_id, consumer="CLOSEOUT")
        self.finalizer = finalizer

    @staticmethod
    def work_key(mission_id: str) -> str:
        return "closeout:" + text(mission_id)

    def classify(self, event: Event) -> tuple[WorkTarget, ...]:
        if event.type == EXPIRY_EVENT:
            return classify_expiry(event, "CLOSEOUT")
        if event.type not in CLOSEOUT_SOURCE_EVENTS:
            return ()
        return (
            WorkTarget(
                self.work_key(event.mission_id),
                fingerprint({"type": event.type, "change_event": fingerprint(event.to_json())}),
            ),
        )

    # -------------------------------------------------------------- readers
    def _root_resolution(
        self, mission: Any
    ) -> tuple[Any | None, list[str], list[str], str | None]:
        """Adopted root GoalResolution for every required root duty, or what is missing.

        A Mission whose plan does not read back (no root binding yet, or a damaged
        plan the original judge refuses) has no root to resolve: reported as
        ``ROOT_NETWORK_UNAVAILABLE`` and NOT_READY, never repaired here.
        """
        from .commit_service import CommitRejected

        try:
            network = self.commit._judgment_network(mission)
        except CommitRejected as error:
            return None, [], [], "ROOT_NETWORK_UNAVAILABLE: " + str(error)[:200]
        if network is None:
            raise AssuranceError("CLOSEOUT_ROOT_NETWORK_UNAVAILABLE")
        semantics = HtnStore(self.store)
        resolutions, missing = [], []
        for duty in dict.fromkeys(network.required_obligations):
            adopted = semantics.adopted_goal_resolution(mission.id, str(duty))
            if adopted is None:
                missing.append(str(duty))
            else:
                resolutions.append(adopted)
        roots = [str(root) for root in network.root_occurrence_ids]
        if missing or not resolutions:
            return None, missing, roots, None
        return resolutions[0], missing, roots, None

    def _evaluate_locked(self, mission: Any, *, now_ms: int) -> dict:
        integer(now_ms)
        from .completion_status import read_current_effect, read_occurrence_completion
        from .scoped_content_review import uses_completion_protocol

        body: dict[str, Any] = {
            "mission_id": mission.id,
            "mission_version": mission.version,
            "mission_status": str(mission.status),
            "evaluated_at_ms": now_ms,
            "root_incarnation_id": self._root(),
            "epochs": read_epochs_locked(self.store.connection, mission.id).to_json(),
        }
        reasons: list[str] = []
        if str(mission.status) != "ACTIVE":
            reasons.append("MISSION_NOT_ACTIVE")
        resolution, missing, roots, unavailable = self._root_resolution(mission)
        body["root_occurrences"] = roots
        body["missing_root_duties"] = missing
        body["root_network"] = unavailable
        if resolution is None:
            reasons.append(
                "ROOT_RESOLUTION_MISSING" if unavailable is None else "ROOT_NETWORK_UNAVAILABLE"
            )
            body.update(state="NOT_READY", resolution_id=None, reasons=reasons)
            return body
        body["resolution_id"] = str(resolution.resolution_id)
        # The Mission judge's own verdict on ``Mission.success_criteria`` is part of
        # the root business requirement; the unique final writer completes from it.
        judgment = recorded_judgment(mission)
        body["success_judgment"] = None if judgment is None else dict(judgment)
        if judgment is None:
            reasons.append("MISSION_JUDGMENT_MISSING")
        if str(resolution.verdict) != "ACCEPT":
            reasons.append("ROOT_RESOLUTION_NOT_ACCEPT")
        if str(resolution.validity) != "CURRENT":
            reasons.append("ROOT_RESOLUTION_NOT_CURRENT")
        unknown_effects: list[str] = []
        unmet: list[str] = []
        if uses_completion_protocol(self.store, mission.id):
            for root in roots:
                status = read_occurrence_completion(self.store, mission.id, root)
                if not status.complete:
                    unmet.append(root)
                for effect_key in status.scope.required_effect_keys:
                    current = read_current_effect(
                        self.store, mission.id, status.scope.spec_hash, str(effect_key)
                    )
                    if current["state"] == "RECONCILIATION_REQUIRED":
                        unknown_effects.append(str(effect_key))
        body["unmet_root_occurrences"] = unmet
        body["unknown_effects"] = unknown_effects
        if unmet:
            reasons.append("ROOT_SCOPE_UNMET")
        open_intents = sorted(
            intent.intent_id
            for intent in self.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            )
            if intent.mission_id == mission.id
        )
        open_reservations = [
            row[0]
            for row in self.store.connection.execute(
                "SELECT subject_id FROM budget_reservations WHERE mission_id=? "
                "AND state='RESERVED' ORDER BY subject_id LIMIT 257",
                (mission.id,),
            ).fetchall()
        ]
        usage = self.commit._ledger.usage_flags(mission.id)
        body.update(
            open_intents=open_intents[:256],
            open_reservations=open_reservations[:256],
            usage_fully_known=bool(usage["usage_fully_known"]),
            budget_conserved=bool(usage["budget_conserved"]),
        )
        # User decision 2026-09-26: a judged Mission whose only remaining drain is the
        # UNKNOWN charge of work that will never run again closes with that charge
        # counted at its upper bound (overcount, never undercount, never freeze).
        # Before, one unknown call kept the reservation held forever and the Mission
        # sat ACTIVE with every criterion met.
        upper = self._upper_bound_plan(mission.id, open_reservations) if (
            not reasons and not unknown_effects and not open_intents
            and (open_reservations or not usage["usage_fully_known"])
        ) else None
        if upper is not None:
            body["usage_counted_at_upper_bound"] = upper
        if reasons:
            body.update(state="NOT_READY", reasons=reasons)
        elif unknown_effects:
            body.update(state="BLOCKED_UNKNOWN", reasons=["EFFECT_UNKNOWN"])
        elif upper is not None:
            body.update(state="READY", reasons=[])
        elif open_intents or open_reservations or not usage["usage_fully_known"]:
            draining = []
            if open_intents:
                draining.append("OPEN_INTENTS")
            if open_reservations:
                draining.append("OPEN_RESERVATIONS")
            if not usage["usage_fully_known"]:
                draining.append("USAGE_UNKNOWN")
            body.update(state="DRAINING", reasons=draining)
        else:
            body.update(state="READY", reasons=[])
        return body

    def _upper_bound_plan(self, mission_id: str, open_reservations: list[str]) -> dict | None:
        """The subjects to count at their upper bound, or None when anything else drains.

        Every open reservation must be held by an UNKNOWN charge, and every unknown
        usage fact / unsettled grant of the Mission must belong to a subject that is
        either counted now or was counted by an earlier closeout.
        """
        ledger = self.commit._ledger
        if any(not ledger.has_unknown_usage(subject) for subject in open_reservations):
            return None
        counted = {
            str(row[0]) for row in self.store.connection.execute(
                "SELECT json_extract(payload_json,'$.subject_id') FROM events "
                "WHERE mission_id=? AND type=?", (mission_id, UPPER_BOUND_EVENT)).fetchall()
        }
        pending = set(open_reservations)
        unknown = {
            str(row[0]) for row in self.store.connection.execute(
                "SELECT subject_id FROM imported_usage WHERE mission_id=? AND unknown=1",
                (mission_id,)).fetchall()
        }
        if self.store.has_table("provider_token_grants"):
            unknown |= {
                str(row[0]) for row in self.store.connection.execute(
                    "SELECT subject_id FROM provider_token_grants WHERE mission_id=? "
                    "AND state IN ('RESERVED','HANDED_OFF','UNKNOWN')", (mission_id,)).fetchall()
            }
        if not unknown <= pending | counted:
            return None
        return {"subjects": sorted(pending), "already_counted": sorted(counted & unknown)}

    def _stable(self, evaluation: Mapping[str, Any]) -> dict:
        return {key: value for key, value in evaluation.items() if key not in self.VOLATILE}

    # -------------------------------------------------------------- writer
    def _write_locked(self, evaluation: dict, *, trigger: dict, claim: WorkClaim) -> AssuranceRef:
        mission_id = evaluation["mission_id"]
        connection = self.store.connection
        row = connection.execute(
            "SELECT * FROM assurance_closeouts WHERE mission_id=?", (mission_id,)
        ).fetchone()
        finalized = row is not None and row["state"] == "FINALIZED"
        if finalized:
            # History never rolls back. Re-evaluations after finalisation are
            # recorded as receipts only; the row keeps its final identity.
            evaluation = {**evaluation, "state": "FINALIZED", "reasons": ["ALREADY_FINALIZED"]}
        check_hash = fingerprint(evaluation)
        check_json = canonical(evaluation)
        now_ms = evaluation["evaluated_at_ms"]
        state = evaluation["state"]
        writes_row = evaluation.get("resolution_id") is not None and not finalized
        row_version = None if row is None else row["row_version"]
        changed = False
        if writes_row:
            if row is None:
                row_version, changed = 1, state != "NOT_READY"
            else:
                changed = (
                    row["state"] != state
                    or row["check_body_hash"] != check_hash
                    or row["resolution_id"] != evaluation["resolution_id"]
                )
            if changed:
                row_version += 1
        receipt = {
            **evaluation,
            "trigger": trigger,
            "work_key": claim.work_key,
            "target_epoch": claim.target_epoch,
            "row_version": row_version,
            "check_body_hash": check_hash,
        }
        receipt_id = "assurance-closeout:" + fingerprint(
            {"mission_id": mission_id, "trigger": trigger, "check_body_hash": check_hash}
        )
        ref = _receipt_ref(self.store, receipt_id, CLOSEOUT_EVENT, mission_id, receipt)
        if writes_row:
            if row is None:
                # First insert is always NOT_READY / version 1 (spec §7.2); the
                # computed state lands as the immediate re-evaluation below.
                connection.execute(
                    "INSERT INTO assurance_closeouts VALUES(?,?,?,?,?,?,?,?)",
                    (
                        mission_id,
                        evaluation["resolution_id"],
                        "NOT_READY",
                        1,
                        check_hash,
                        check_json,
                        receipt_id,
                        now_ms,
                    ),
                )
            if changed:
                connection.execute(
                    "UPDATE assurance_closeouts SET resolution_id=?,state=?,row_version=?,"
                    "check_body_hash=?,check_body_json=?,last_receipt_id=?,updated_at_ms=? "
                    "WHERE mission_id=?",
                    (
                        evaluation["resolution_id"],
                        state,
                        row_version,
                        check_hash,
                        check_json,
                        receipt_id,
                        now_ms,
                        mission_id,
                    ),
                )
        self.commit._emit(
            CLOSEOUT_EVENT,
            mission_id,
            key=receipt_id,
            payload={
                "state": state,
                "reasons": evaluation["reasons"],
                "resolution_id": evaluation.get("resolution_id"),
                "check_body_hash": check_hash,
                "receipt_ref": ref.to_json(),
            },
        )
        upper = evaluation.get("usage_counted_at_upper_bound")
        if state == "READY" and upper and not finalized:
            for subject in upper["subjects"]:
                settled = self.commit._ledger.settle_at_upper_bound(subject_id=subject)
                self.commit._emit(UPPER_BOUND_EVENT, mission_id, key="upper-bound:" + subject, payload={
                    "subject_id": subject, "reason": "unknown_usage",
                    "counted_tokens": settled["settled_tokens"],
                    "counted_cost_micros": settled["settled_cost_micros"],
                    "closeout_receipt_ref": ref.to_json()})
        if state == "READY" and self.finalizer is not None:
            final = self.finalizer(mission_id, evaluation)
            if final is not None and not isinstance(final, AssuranceRef):
                raise AssuranceError("WORK_COMMIT_RECEIPT_REQUIRED")
        return ref

    async def prepare(self, claim: WorkClaim) -> PreparedAssuranceWork | AssuranceWait:
        if self.store.connection.in_transaction:
            raise AssuranceError("ASSURANCE_PREPARATION_INSIDE_TRANSACTION")
        with self.store.read_view():
            mission = self._require_mission(claim.mission_id)
            event = _trigger(self.store, claim)
            self._check_claim(claim, event)
            preview = self._stable(
                self._evaluate_locked(mission, now_ms=int(self.store.now * 1000))
            )
        trigger = _trigger_json(event)

        def commit() -> AssuranceRef:
            with atomic(self.store):
                current = self._require_mission(claim.mission_id)
                evaluation = self._evaluate_locked(current, now_ms=int(self.store.now * 1000))
                if self._stable(evaluation) != preview:
                    raise AssuranceError("RECHECK_REQUIRED")
                return self._write_locked(evaluation, trigger=trigger, claim=claim)

        return PreparedAssuranceWork(commit)


# -------------------------------------------------------------------- NOTIFY
class AssuranceNotifyConsumer(_ConsumerBase):
    """At-least-once local status transport for the final writer's request.

    ``transport(payload)`` is the deployment's current Host push (the original
    tick→Host event chain); it receives ``{mission_id, event_id, state_version}``
    only. A raising transport keeps the work WAITING under the shared retry cap;
    a successful send records one original receipt so a replay is not re-sent.
    The execution root gate is checked before every send (spec §7.3).
    """

    def __init__(
        self,
        commit: Any,
        *,
        tenant_id: str,
        transport: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        super().__init__(commit, tenant_id=tenant_id, consumer="NOTIFY")
        self.transport = transport

    @staticmethod
    def _body(event: Event) -> dict:
        return fields(
            dict(event.payload), {"final_event_id", "state_version"}, {"final_event_type"}
        )

    def classify(self, event: Event) -> tuple[WorkTarget, ...]:
        if event.type != NOTIFICATION_EVENT:
            return ()
        body = self._body(event)
        key = "notify:" + event.mission_id + ":" + text(body["final_event_id"])
        return (WorkTarget(key, fingerprint({"type": event.type, "payload": body})),)

    async def prepare(self, claim: WorkClaim) -> PreparedAssuranceWork | AssuranceWait:
        if self.store.connection.in_transaction:
            raise AssuranceError("ASSURANCE_PREPARATION_INSIDE_TRANSACTION")
        receipt_id = "assurance-notified:" + claim.work_key
        with self.store.read_view():
            mission = self._require_mission(claim.mission_id)
            event = _trigger(self.store, claim)
            self._check_claim(claim, event)
            body = self._body(event)
            final = self.store.connection.execute(
                "SELECT event_id FROM events WHERE event_id=? AND mission_id=?",
                (body["final_event_id"], mission.id),
            ).fetchone()
            if final is None:
                raise AssuranceError("SOURCE_UNAVAILABLE", "final event")
            self._root()
            already = self.store.get_receipt(receipt_id)
        payload = {
            "mission_id": mission.id,
            "event_id": body["final_event_id"],
            "state_version": integer(body["state_version"]),
        }
        if already is None:
            if self.transport is None:
                raise AssuranceError("NOTIFY_TRANSPORT_UNBOUND")
            # The send happens outside the Store lock; a crash between the send
            # and the receipt re-sends (at-least-once, spec §7.3). The Host
            # de-duplicates by event_id.
            self.transport(payload)
        receipt_body = {
            "mission_id": mission.id,
            "notification_event_id": event.id,
            "final_event_id": body["final_event_id"],
            "state_version": payload["state_version"],
            "delivered_payload": payload,
        }

        def commit() -> AssuranceRef:
            with atomic(self.store):
                self._require_mission(claim.mission_id)
                self._root()
                ref = _receipt_ref(self.store, receipt_id, NOTIFIED_EVENT, mission.id, receipt_body)
                self.commit._emit(
                    NOTIFIED_EVENT,
                    mission.id,
                    key=receipt_id,
                    payload={"receipt_ref": ref.to_json(), **payload},
                )
                return ref

        return PreparedAssuranceWork(commit)


__all__ = (
    "ACTIVATION_EVENT",
    "AssuranceCloseoutConsumer",
    "AssuranceNotifyConsumer",
    "AssuranceValidityConsumer",
    "CLOSEOUT_EVENT",
    "CLOSEOUT_SOURCE_EVENTS",
    "DIAGNOSTIC_EVENTS",
    "NOTIFICATION_EVENT",
    "NOTIFIED_EVENT",
    "VALIDITY_CHECKED_EVENT",
    "VALIDITY_SOURCE_EVENTS",
)
