# SPDX-License-Identifier: Apache-2.0
"""Explicit experiment issuer for the frozen AppWorld preparation source slot.

This is not a product auto-approval path. It submits the experiment's declared
command through the original human-caller API only after actual preparation has
been accepted. Review and action approval policies still run in the runtime.
"""
from __future__ import annotations

from typing import Any

from ..api.operation_intents import OperationIntentApi
from ..contracts.models import ContractError
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from ..governance.permissions import Principal
from ..orchestrator.operation_completion import OperationCompletionReader
from ..planning.htn.seed_methods.loader import seed_content_hash
from ..storage.htn_store import HtnStore
from ..storage.operation_intent_store import OperationIntentStore


class AppWorldExperimentCommands:
    def __init__(self, *, principal: Principal, contract: dict[str, Any]):
        if principal.kind != "human" or contract.get("mode") != "REQUIRED_EFFECTS":
            raise ContractError("AppWorld experiment needs its explicit completion authorization")
        effects = contract.get("effects")
        if (not isinstance(effects, list) or len(effects) != 1
                or effects[0].get("source_slot_key") != "appworld-program"
                or effects[0].get("obligation_id") != "$ROOT_OBLIGATION"):
            raise ContractError("AppWorld experiment must freeze one root program source slot")
        self.principal, self.contract = principal, contract

    def advance(self, loop: Any, mission: Any) -> None:
        if str(mission.status) != "ACTIVE":
            return
        store = loop.store
        htn = HtnStore(store)
        requirements = htn.latest_requirements_revision(mission.id)
        if requirements is None or htn.active_plan_revision(mission.id) is None:
            return
        spec = OperationCompletionReader(store).read_requirements(mission.id, TypedRef(
            TypedRefKind.REQUIREMENTS, str(requirements.revision_id), requirements.revision,
            requirements.content_hash()))
        expected = dict(self.contract["effects"][0])
        expected["obligation_id"] = "h8-duty-" + mission.id
        if (len(spec.effects) != 1 or spec.effects[0].to_json() != expected
                or spec.content_criterion_ids != tuple(self.contract["content_criterion_ids"])):
            raise ContractError("experiment command does not match the current approved Spec")
        # An unresolved/rejected/completed intent is not a new authorization. A
        # second business action needs an explicit successor experiment command.
        for row in OperationIntentStore(store).for_mission(mission.id):
            if row["binding"]["completion"]["effect_key"] == expected["effect_key"]:
                return
        candidates = []
        for acceptance in htn.list_acceptances(mission.id):
            if str(acceptance.validity) != "CURRENT":
                continue
            semantic = htn.latest_task_semantics(str(acceptance.task_id))
            producer_refs = {("appworld.op-prepare-action", 1, seed_content_hash("appworld.op-prepare-action", 1)),
                             ("appworld.op-prepare-search-action", 1, seed_content_hash("appworld.op-prepare-search-action", 1))}
            if semantic is None or semantic.operator_ref is None or (
                semantic.operator_ref.id, semantic.operator_ref.version, semantic.operator_ref.content_hash) not in producer_refs:
                continue
            for reference in acceptance.artifact_refs:
                artifact = store.get_artifact(reference.id)
                if artifact is not None and artifact.path == "actions/appworld-request.json":
                    candidates.append((acceptance, artifact))
        if not candidates:
            return
        if len(candidates) != 1:
            raise ContractError("AppWorld program source slot has ambiguous preparation")
        acceptance, artifact = candidates[0]
        OperationIntentApi(loop, tenant_id=mission.tenant_id, principal=self.principal).submit({
            "schema_version": 2, "mission_id": mission.id,
            "idempotency_key": "h8-program:" + mission.id,
            "intent_source": {"kind": "USER_COMMAND"}, "supersedes_intent_id": None,
            "candidate_artifact_ref": TypedRef(TypedRefKind.ARTIFACT, artifact.id,
                artifact.version, artifact.content_hash).to_json(),
            "prepared_acceptance_refs": [TypedRef(TypedRefKind.ACCEPTANCE,
                str(acceptance.acceptance_id), 1, content_hash_of(acceptance.to_json())).to_json()],
            "completion_slot": {"spec_hash": spec.content_hash(), "effect_key": expected["effect_key"]}})
