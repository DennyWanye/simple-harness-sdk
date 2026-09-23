# SPDX-License-Identifier: Apache-2.0
"""Original Critic runner using durable Assurance transport and REVIEW effects.

This adapter never parses a model response into a successful verification layer.
Only the original official importer can produce the record consumed here. The
layer remains a historical verdict; acceptance needs independent current proof.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Callable, Any

from simple_harness.agents import AgentConfig, AgentLimits
from simple_harness.agents.context.budget import policy_hash
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer

from ..assurance.codec import AssuranceError, decode, fingerprint
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.review_input import REVIEW_INSTRUCTIONS
from ..assurance.reviews import REVIEW_CODEC_VERSION
from ..contracts import TERMINAL_ATTEMPT, TERMINAL_MISSION, TERMINAL_TASK, ContractError
from ..runtime.tool_gateway import ASSURANCE_EVIDENCE_TOOLS
from ..storage.assurance_reads import AssuranceReader
from ..storage.htn_store import HtnStore
from ..verification.critics import CriticVerdict
from .assurance_content_review import ensure_task_content_review
from .assurance_review_collect import collect_assurance_review
from .assurance_review_consumer import AssuranceReviewConsumer
from .assurance_review_import import read_official_review_binding_locked

# One review turn: the initial request plus a bounded number of tool rounds.
# The per-call cap on the gateway binding is MAX_EVIDENCE_TOOL_CALLS.
REVIEW_MODEL_CALLS = 6
REVIEW_TOOL_CALLS = 8


class AssuranceReviewRuntime:
    def __init__(
        self,
        orchestrator: Any,
        consumer: AssuranceReviewConsumer,
        *,
        check_policy_projector: Callable[[str], None] | None = None,
    ) -> None:
        if consumer.commit is not orchestrator.commit or consumer.store is not orchestrator.store:
            raise AssuranceError("ASSURANCE_STORE_MISMATCH")
        self.orchestrator = orchestrator
        self.consumer = consumer
        self.store = consumer.store
        # Deployment port: projects the frozen Scope's check policy under the
        # Host's caller right before the first review preparation needs it.
        self._check_policy_projector = check_policy_projector
        from ..verification.reviewer_evidence_tools import ReviewerEvidenceTools

        self.evidence_tools = ReviewerEvidenceTools(self)

    def context_pin(self, profile_id: str) -> Pin:
        ports = self.orchestrator.assembled.pool(profile_id).bridge.runtime.ports
        tokenizer = ports.tokenizer or UpperBoundTokenizer()
        return Pin(
            "base-agent-context:" + profile_id,
            1,
            policy_hash(
                ports.context_policy, tokenizer_fingerprint=tokenizer.fingerprint, model=ports.model
            ),
        )

    def install(self) -> AssuranceReviewConsumer:
        """Bind original runner/handoff; caller still installs all four consumers.

        Startup may call this before execution pools are opened. The actual pool,
        root and complete tick are required again on each later runtime entry.
        """
        from .assurance_review_handoff import AssuranceReviewHandoff
        from .assurance_settlement import AssuranceSettlement

        orch = self.orchestrator
        if orch._assurance_reviews not in (None, self):
            raise AssuranceError("ASSURANCE_REVIEW_RUNTIME_ALREADY_BOUND")
        if (
            orch.commit._assurance_review_handoff is not None
            and orch._assurance_reviews is not self
        ):
            raise AssuranceError("ASSURANCE_REVIEW_RUNTIME_ALREADY_BOUND")
        orch._assurance_reviews = self
        if orch.commit._assurance_review_handoff is None:
            orch.commit._assurance_review_handoff = AssuranceReviewHandoff(self)
        if orch.commit._assurance_settlement is None:
            orch.commit._assurance_settlement = AssuranceSettlement(orch)
        gateway = getattr(getattr(orch, "assembled", None), "gateway", None)
        if gateway is not None and hasattr(gateway, "assurance_evidence_reader"):
            self.evidence_tools.install(gateway)
        return self.consumer

    def task_record(self, mission_id: str, attempt_id: str):
        """Exact official source for this Result; no latest-Review fallback."""
        result = self.store.find_result_for_attempt(attempt_id)
        if result is None or result.envelope.mission_id != mission_id:
            raise AssuranceError("REVIEW_RESULT_SOURCE_MISSING")
        rows = self.store.connection.execute(
            "SELECT package_id FROM assurance_review_bindings WHERE mission_id=? "
            "AND subject_hash=? AND json_extract(binding_json,'$.subject.purpose')='TASK_CONTENT'",
            (mission_id, fingerprint(result.envelope.to_json())),
        ).fetchall()
        if len(rows) > 1:
            raise AssuranceError("REVIEW_RESULT_SOURCE_AMBIGUOUS")
        if not rows:
            return None
        record = HtnStore(self.store).official_review_record(rows[0][0])
        if record is not None:
            read_official_review_binding_locked(
                self.orchestrator.commit, self.consumer.tenant_id, record
            )
        return record

    def provenance(self, mission_id: str, attempt_id: str) -> dict[str, str]:
        with self.store.read_view():
            record = self.task_record(mission_id, attempt_id)
            if record is None:
                return {}
            rows = self.store.connection.execute(
                "SELECT i.dispatch_intent_id FROM assurance_review_invocations i "
                "JOIN assurance_review_record_bindings r "
                "ON r.mission_id=i.mission_id AND r.review_key=i.review_key "
                "AND json_extract(r.binding_json,'$.invocation_ordinal')=i.ordinal "
                "WHERE r.record_id=?",
                (str(record.record_id),),
            ).fetchall()
            if len(rows) != 1:
                raise AssuranceError("REVIEW_RESULT_SOURCE_AMBIGUOUS")
            intent = self.store.get_intent(rows[0][0])
            if intent is None or intent.config.get("attempt_id") != attempt_id:
                raise AssuranceError("REVIEW_RESULT_SOURCE_MISMATCH")
            return {"critic_intent_id": intent.intent_id, "verifier_version": REVIEW_CODEC_VERSION}

    def _verdict(self, record: Any) -> CriticVerdict:
        reader = AssuranceReader(
            self.store, tenant_id=self.consumer.tenant_id, mission_id=record.binding.mission_id
        )
        read_official_review_binding_locked(
            self.orchestrator.commit, self.consumer.tenant_id, record
        )
        manifest = decode(
            reader.read_exact_metadata(
                AssuranceRef(
                    "input_manifest",
                    Pin(record.evidence_manifest_hash, 0, record.evidence_manifest_hash),
                )
            ).body_json
        )
        verdict = manifest["effective_verdict"]
        if verdict == "INCONCLUSIVE":
            raise ContractError("Assurance review is INCONCLUSIVE; no conclusive verification")
        if verdict not in {"ACCEPT", "REWORK", "REJECTED"}:
            raise ContractError("Assurance official verdict cannot drive this verification")
        return CriticVerdict(
            "PASS" if verdict == "ACCEPT" else "FAIL",
            tuple(
                {
                    "severity": item["severity"].lower(),
                    "description": item["reason"],
                    "criterion_id": item["criterion_id"],
                }
                for item in manifest["findings"]
            )
            + tuple(
                {
                    "severity": "blocker" if item["effective_grade"] == "FAIL" else "info",
                    "description": item["reason"],
                    "criterion_id": item["criterion_id"],
                }
                for item in manifest["criteria"]
                if item["effective_grade"] != "PASS"
            ),
            tuple(
                {"criterion": item["criterion_id"], "met": item["effective_grade"] == "PASS"}
                for item in manifest["criteria"]
            ),
            {
                "official_review_record_id": str(record.record_id),
                "evidence_manifest_hash": record.evidence_manifest_hash,
            },
        )

    def _licensed(self, record: Any) -> CriticVerdict:
        """Historical verdict; the licence is prepared later by the acceptance path.

        The verdict itself is not a licence. The router still records layers
        (inventoried sources) after this returns, so the current ACCEPT use is
        computed by ``CommitService.accept_result`` immediately before its UoW.
        A deployment without a validity evaluator is refused here rather than
        producing an unlicensed PASS.
        """
        with self.store.read_view():
            verdict = self._verdict(record)
        if not verdict.passed:
            return verdict
        if getattr(self.orchestrator.commit, "_assurance_validity", None) is None:
            raise AssuranceError("ASSURANCE_VALIDITY_UNBOUND")
        return verdict

    # ------------------------------------------------ the other five purposes
    def _purpose_config(self, mission: Any, name: str) -> tuple[Any, dict[str, Any], Any]:
        orch = self.orchestrator
        if mission.tenant_id != self.consumer.tenant_id:
            raise AssuranceError("ASSURANCE_TENANT_MISMATCH")
        decision = orch._route_service("critic", mission.id)
        config = {
            **orch._service_config(decision),
            "prompt_version": REVIEW_CODEC_VERSION,
            "agent_config": AgentConfig(
                name=name,
                instructions=REVIEW_INSTRUCTIONS,
                model_profile_ref=decision.profile_id,
                # §4: this profile's template explicitly carries the two read-only
                # evidence tools; the legacy root reviewer keeps tool_names=().
                tool_names=ASSURANCE_EVIDENCE_TOOLS,
                limits=AgentLimits(
                    max_model_calls_per_turn=REVIEW_MODEL_CALLS,
                    max_tool_calls_per_turn=REVIEW_TOOL_CALLS,
                    turn_deadline_seconds=orch._config.turn_deadline_seconds,
                ),
            ).to_json(),
        }
        reservation = orch._reservation(orch._config.critic_reserve_tokens, decision.profile_id)
        return decision, config, reservation

    def _ensure_purpose(
        self,
        mission: Any,
        *,
        name: str,
        package: Any,
        subject: Any,
        requirements: Any,
        request_command_id: str,
        allow_in_transaction: bool = False,
    ) -> Any:
        from .assurance_purpose_reviews import prepare_purpose_review

        decision, config, reservation = self._purpose_config(mission, name)
        return prepare_purpose_review(
            self.orchestrator.commit,
            tenant_id=mission.tenant_id,
            mission_id=mission.id,
            package=package,
            subject=subject,
            requirements=requirements,
            principal_id=self.consumer.principal_id,
            authority=self.consumer.authority,
            cas=self.consumer.cas,
            config=config,
            context_policy_ref=self.context_pin(decision.profile_id),
            reservation=reservation,
            request_command_id=request_command_id,
            allow_in_transaction=allow_in_transaction,
        )

    def ensure_method_plan(
        self, mission: Any, *, task_id: str, method_ref: Pin, producer_agent_ids: tuple[str, ...]
    ) -> Any:
        """METHOD_PLAN from the original method admission (planning subject + registry)."""
        from .assurance_purpose_reviews import method_plan_package

        package, subject, requirements = method_plan_package(
            self.store,
            mission_id=mission.id,
            task_id=task_id,
            method_ref=method_ref,
            producer_agent_ids=producer_agent_ids,
        )
        return self._ensure_purpose(
            mission,
            name="assurance-method-plan-review",
            package=package,
            subject=subject,
            requirements=requirements,
            request_command_id="method-plan-review:" + str(package.package_id),
            allow_in_transaction=True,
        )

    def ensure_composition(
        self, mission: Any, *, package: Any, occurrence_id: str, accepted: Mapping[str, Any]
    ) -> Any:
        """COMPOSITION from the original composition assembly's package."""
        from .assurance_purpose_reviews import composition_subject

        subject, requirements = composition_subject(
            self.store,
            mission_id=mission.id,
            package=package,
            occurrence_id=occurrence_id,
            accepted=accepted,
        )
        return self._ensure_purpose(
            mission,
            name="assurance-composition-review",
            package=package,
            subject=subject,
            requirements=requirements,
            request_command_id="composition-review:" + str(package.package_id),
        )

    def ensure_action_proposal(self, mission: Any, *, draft: Any, sources: Any, payloads: Any) -> Any:
        """ACTION_PROPOSAL from T0's frozen draft, inside T0's own UoW."""
        from .assurance_purpose_reviews import action_proposal_subject

        subject, requirements = action_proposal_subject(
            self.store, mission_id=mission.id, package=draft.package, sources=sources, payloads=payloads
        )
        return self._ensure_purpose(
            mission,
            name="assurance-action-proposal-review",
            package=draft.package,
            subject=subject,
            requirements=requirements,
            request_command_id="action-proposal-review:" + str(draft.package.package_id),
            allow_in_transaction=True,
        )

    def ensure_operation_outcome(self, mission: Any, *, prepared: Any) -> Any:
        """OPERATION_OUTCOME from the original outcome readback, inside its persistence UoW."""
        from .assurance_purpose_reviews import operation_outcome_subject

        subject, requirements = operation_outcome_subject(
            self.store, mission_id=mission.id, prepared=prepared
        )
        return self._ensure_purpose(
            mission,
            name="assurance-operation-outcome-review",
            package=prepared.package,
            subject=subject,
            requirements=requirements,
            request_command_id="operation-outcome-review:" + str(prepared.package.package_id),
            allow_in_transaction=True,
        )

    def ensure_mission_final(self, mission: Any, *, package: Any, dispatch: Any) -> Any:
        """MISSION_FINAL from the root coordinator's cut package."""
        from .assurance_purpose_reviews import mission_final_subject

        subject, requirements = mission_final_subject(
            self.store, mission_id=mission.id, package=package, dispatch=dispatch
        )
        return self._ensure_purpose(
            mission,
            name="assurance-mission-final-review",
            package=package,
            subject=subject,
            requirements=requirements,
            request_command_id="mission-final-review:" + str(package.package_id),
        )

    async def run_task(self, mission: Any, task: Any, *, attempt_id: str) -> CriticVerdict:
        orch = self.orchestrator
        tick = orch._assurance_tick
        if tick is None or tick.consumers.get("REVIEW") is not self.consumer:
            raise AssuranceError("ASSURANCE_REVIEW_CONSUMER_UNBOUND")
        if mission.tenant_id != self.consumer.tenant_id:
            raise AssuranceError("ASSURANCE_TENANT_MISMATCH")
        with self.store.read_view():
            record = self.task_record(mission.id, attempt_id)
            if record is None:
                result = self.store.find_result_for_attempt(attempt_id)
        if record is not None:
            return self._licensed(record)
        with self.store.read_view():
            if result is None or result.envelope.task_id != task.id:
                raise AssuranceError("REVIEW_RESULT_SOURCE_MISMATCH")
            prior = self.store.connection.execute(
                "SELECT i.dispatch_intent_id FROM assurance_review_invocations i "
                "JOIN assurance_review_bindings b USING(mission_id,review_key) "
                "WHERE b.mission_id=? AND b.subject_hash=? AND i.ordinal=1 "
                "AND json_extract(b.binding_json,'$.subject.purpose')='TASK_CONTENT'",
                (mission.id, fingerprint(result.envelope.to_json())),
            ).fetchone()
            invocation = None
            if prior is not None:
                from .assurance_review_transport import read_review_invocation_locked

                invocation, _ = read_review_invocation_locked(
                    orch.commit,
                    AssuranceReader(self.store, tenant_id=mission.tenant_id, mission_id=mission.id),
                    prior[0],
                )
        selection = orch.commit.selection_deadline(attempt_id)
        deadline = min(
            self.store.now + orch._critic_wait, selection if selection is not None else float("inf")
        )
        if deadline <= self.store.now:
            raise ContractError("selection deadline elapsed before Assurance review")
        if invocation is None:
            decision = orch._route_service("critic", mission.id)
            fields, reservation = protected_critic_budget(orch, decision, task.id, attempt_id)
            config = {
                **orch._service_config(decision),
                **fields,
                "attempt_id": attempt_id,
                "prompt_version": REVIEW_CODEC_VERSION,
                "agent_config": AgentConfig(
                    name="assurance-content-review",
                    instructions=REVIEW_INSTRUCTIONS,
                    model_profile_ref=decision.profile_id,
                    tool_names=ASSURANCE_EVIDENCE_TOOLS,
                    limits=AgentLimits(
                        max_model_calls_per_turn=REVIEW_MODEL_CALLS,
                        max_tool_calls_per_turn=REVIEW_TOOL_CALLS,
                        turn_deadline_seconds=min(
                            orch._config.turn_deadline_seconds, deadline - self.store.now
                        ),
                    ),
                ).to_json(),
            }
            if self._check_policy_projector is not None and not self.store.connection.in_transaction:
                # The projector approves through the ordinary verb (receipted,
                # replay-safe); a projection failure surfaces below as the same
                # CHECK_POLICY_UNRESOLVED the review would raise without it.
                self._check_policy_projector(mission.id)
            invocation = ensure_task_content_review(
                orch.commit,
                tenant_id=mission.tenant_id,
                result_id=result.envelope.id,
                principal_id=self.consumer.principal_id,
                authority=self.consumer.authority,
                cas=self.consumer.cas,
                config=config,
                context_policy_ref=self.context_pin(decision.profile_id),
                reservation=reservation,
            )
        review_key = invocation.to_json()["review_key"]
        while self.store.now < deadline:
            self._require_live(mission.id, task.id, attempt_id)
            with self.store.read_view():
                record = self.task_record(mission.id, attempt_id)
            if record is not None:
                return self._licensed(record)
            with self.store.read_view():
                row = self.store.connection.execute(
                    "SELECT dispatch_intent_id FROM assurance_review_invocations "
                    "WHERE mission_id=? AND review_key=? ORDER BY ordinal DESC LIMIT 1",
                    (mission.id, review_key),
                ).fetchone()
                intent = self.store.get_intent(row[0])
            if intent.state not in {"SETTLED", "FAILED"}:
                intent, answer = await orch._await_service_turn(
                    intent, deadline, attempt_id=attempt_id
                )
                if answer is None:
                    # SUBMITTED and its held reservation remain collectable. A
                    # timeout does not create a format-repair ordinal.
                    await orch._cancel_turn(intent)
                    raise ContractError("Assurance review awaits original-call reconciliation")
                if orch._critic_subject_stopped(intent):
                    await orch._collect_after_stop(intent)
                    raise ContractError("Assurance review subject stopped")
                await collect_assurance_review(orch, intent)
                orch.assembled.gateway.unbind(intent.agent_id)
            # Same durable four-consumer tick as the outer loop. It only prepares
            # effects; a repair intent goes through the original dispatcher above.
            await tick.tick()
            self._raise_final_failure(review_key)
            orch._hold_lease(attempt_id)
            await asyncio.sleep(orch._poll)
        raise ContractError("Assurance review import wait window elapsed")

    def _require_live(self, mission_id: str, task_id: str, attempt_id: str) -> None:
        self.orchestrator._require_assurance_execution_root()
        mission, task = self.store.get_mission(mission_id), self.store.get_task(task_id)
        attempt = self.store.get_attempt(attempt_id)
        if (
            mission is None
            or task is None
            or attempt is None
            or mission.status in TERMINAL_MISSION
            or task.status in TERMINAL_TASK
            or attempt.status in TERMINAL_ATTEMPT
        ):
            raise ContractError("Assurance review subject stopped")

    def _raise_final_failure(self, review_key: str) -> None:
        failed = self.store.connection.execute(
            "SELECT json_extract(receipt_json,'$.classification') FROM commit_receipts "
            "WHERE kind='AssuranceReviewClassified' AND json_extract(receipt_json,'$.review_key')=? "
            "AND json_extract(receipt_json,'$.classification')<>'READY_FOR_CURRENT_REVIEW' LIMIT 1",
            (review_key,),
        ).fetchone()
        if failed is not None:
            raise ContractError("Assurance review cannot be imported: " + failed[0])
        row = self.store.connection.execute(
            "SELECT kind FROM commit_receipts WHERE subject_id=? AND kind IN "
            "('AssuranceReviewImportRejected','AssuranceReviewFormatExhausted','AssuranceReviewLateTurn') LIMIT 1",
            (review_key,),
        ).fetchone()
        if row is not None:
            raise ContractError("Assurance review stopped: " + row[0])
        row = self.store.connection.execute(
            "SELECT wait_reason FROM assurance_pending_work WHERE consumer='REVIEW' "
            "AND work_key IN (?,?) AND wait_reason='MANUAL_REQUIRED' LIMIT 1",
            ("review-import:" + review_key + ":1", "review-import:" + review_key + ":2"),
        ).fetchone()
        if row is not None:
            raise ContractError("Assurance review preparation requires manual resolution")


def protected_critic_budget(orch: Any, decision: Any, task_id: str, attempt_id: str):
    """Retain the original first-Critic protected-tail contract and account."""
    from ..runtime.first_request_budget import FirstRequestBudget, ProviderInputCap

    worker = orch.store.get_intent_for_subject(attempt_id)
    if worker is None:
        raise ContractError("FIRST Critic has no original Worker intent")
    frozen = worker.config.get("first_critic_budget")
    if frozen is not None:
        if not isinstance(frozen, Mapping):
            raise ContractError("frozen FIRST Critic budget is malformed")
        cap = ProviderInputCap.from_json(frozen.get("provider_input_cap"))
        actual = orch._first_critic_budget(decision)
        if not isinstance(actual, FirstRequestBudget):
            raise ContractError("FIRST Critic budget unavailable")
        reservation = orch._first_critic_reservation(actual, decision.profile_id)
        if (
            actual.provider_input_cap != cap
            or frozen.get("output_ceiling") != actual.output_ceiling
            or frozen.get("minimum_tokens") != actual.minimum_tokens
            or frozen.get("cost_micros") != reservation.cost_micros
        ):
            raise ContractError("FIRST Critic route or cap differs from protected tail")
        return {
            "provider_input_cap": cap.to_json(),
            "provider_output_ceiling": actual.output_ceiling,
            "provider_first_cost_micros": reservation.cost_micros,
        }, reservation
    from .event_handler import SYSTEM_CRITIC_MODEL_CALLS

    reservation = (
        orch._system_reservation(
            orch._config.critic_reserve_tokens, decision.profile_id, SYSTEM_CRITIC_MODEL_CALLS
        )
        if orch.commit.system_task_hold(task_id) is not None
        else orch._reservation(orch._config.critic_reserve_tokens, decision.profile_id)
    )
    return {
        "first_critic_budget_unknown": worker.config.get(
            "first_critic_budget_unknown", "original_intent_has_no_first_cap"
        )
    }, reservation
