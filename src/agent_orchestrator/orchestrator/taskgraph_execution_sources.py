# SPDX-License-Identifier: Apache-2.0
"""Read actual local execution inputs once; do not substitute planning authority.

LocalExecutionSources deliberately is not a complete ExecutionReadContext: the
installed H1 producer must supply reliable runtime imports, Operation facts and
current execution authority before this data can authorize dispatch or Commit.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, TypeVar

from simple_harness.contracts import canonical_json

from ..artifacts.input_bindings import AcceptedOutputsIndex, ResolutionPolicy
from ..contracts.evidence_state import ValidityWitness
from ..contracts.htn import TaskSemanticBindingV1
from ..contracts.models import sha256_hex
from ..contracts.obligations import Obligation, ObligationAccountView
from ..governance.budgets import AccountSnapshot
from ..graph.execution_contracts import CompleteRead
from ..graph.revision_pins import DemandRef
from .taskgraph_demands import IndependentDemand, read_independent_demands
from ..planning.htn.applicability import CapabilitySnapshot
from ..runtime.planning_operations import SourceUnavailable
from ..runtime.taskgraph_local_work import LocalWorkFacts, read_local_work
from ..storage.htn_store import HtnStore, StoredMethod
from ..storage.obligation_store import ObligationStore
from .hierarchical_dispatch import HierarchicalDispatch, NetworkView
from .taskgraph_sources import SeedStructuralReadContext, StructuralReadContext, TaskGraphSources


def _document(value: Any) -> Any:
    """Stable source serialization; never repr/stringify unknown runtime objects."""
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, Enum):
        return value.value
    method = getattr(value, "to_json", None)
    if callable(method):
        return _document(method())
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _document(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        # AcceptedOutputsIndex uses (occurrence, port) keys. Preserve the complete
        # key, rather than joining it with an ambiguous delimiter.
        if all(isinstance(key, str) for key in value):
            return {str(key): _document(item) for key, item in value.items()}
        pairs = [[_document(key), _document(item)] for key, item in value.items()]
        return sorted(pairs, key=canonical_json)
    if isinstance(value, (tuple, list)):
        return [_document(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_document(item) for item in value), key=canonical_json)
    # NewType identifiers remain str subclasses in some deployments.
    if isinstance(value, str):
        return str(value)
    raise SourceUnavailable("taskgraph_source_codec_missing", detail=type(value).__name__)


T = TypeVar("T")


@dataclass(frozen=True, slots=True, kw_only=True)
class WitnessSources:
    witnesses: Mapping[str, ValidityWitness]
    starts: Mapping[str, Mapping[str, ValidityWitness]]
    licences: Mapping[str, Mapping[str, ValidityWitness]]


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalExecutionSources:
    structure: StructuralReadContext | SeedStructuralReadContext
    view: NetworkView
    current_task_bindings: CompleteRead[tuple[TaskSemanticBindingV1, ...]]
    current_method_registrations: CompleteRead[tuple[StoredMethod, ...]]
    witnesses: CompleteRead[WitnessSources]
    scope_epochs: CompleteRead[tuple[tuple[str, int], ...]]
    accepted_outputs: CompleteRead[AcceptedOutputsIndex]
    source_input_policy: CompleteRead[ResolutionPolicy]
    target_rules: CompleteRead[dict[str, Any]]
    obligations: CompleteRead[tuple[Obligation, ...]]
    obligation_accounts: CompleteRead[tuple[ObligationAccountView, ...]]
    active_demands: CompleteRead[tuple[DemandRef, ...]]
    independent_obligations: CompleteRead[tuple[IndependentDemand, ...]]
    budget_snapshots: CompleteRead[tuple[AccountSnapshot, ...]]
    deployment_capabilities: CompleteRead[CapabilitySnapshot]
    local_work: CompleteRead[LocalWorkFacts]

    def source_digests(self) -> tuple[tuple[str, str], ...]:
        return tuple((field.name, getattr(self, field.name).source_digest)
                     for field in fields(self) if field.name not in {"structure", "view"})


class TaskGraphLocalExecutionReader:
    def __init__(self, sources: TaskGraphSources, hierarchical: Callable[[str], HierarchicalDispatch]) -> None:
        if not callable(hierarchical):
            raise ValueError("a Mission dispatcher resolver is required")
        self.sources, self.dispatch_for = sources, hierarchical
        self.store = sources.store

    def read(self, mission_id: str, *, materializing: bool = False) -> LocalExecutionSources:
        with self.store.read_view() as db:
            dispatcher = self.dispatch_for(mission_id)
            if dispatcher.store is not self.store or dispatcher.commit.store is not self.store:
                raise SourceUnavailable("taskgraph_execution_store_mismatch")
            structure: StructuralReadContext | SeedStructuralReadContext
            active = db.execute("SELECT revision FROM plan_revisions WHERE mission_id=? AND state='ACTIVE'", (mission_id,)).fetchall()
            if not active:
                structure = self.sources.read_seed_structure(mission_id, dispatcher.seed_network(mission_id))
            else:
                structure = self.sources.read_structure(mission_id)
            sequence = structure.token.through_seq

            def source(channel: str, value: T) -> CompleteRead[T]:
                document = _document(value)
                if channel == "input_policy":
                    # Time participates in witness evaluation, not in the identity
                    # of the installed policy. Otherwise every preview is stale one
                    # millisecond later even when all policy/epoch facts agree.
                    document = {key: item for key, item in document.items() if key != "now_ms"}
                return CompleteRead(value=value, source_id=f"{mission_id}:{channel}",
                                    source_digest=sha256_hex(document), through_seq=sequence)

            world = dispatcher.planning
            if world is None:
                raise SourceUnavailable("taskgraph_planning_world_missing")
            now_ms = int(self.store.now * 1000)
            view = dispatcher.read(mission_id, now_ms=now_ms)
            if int(view.network.plan_revision) != structure.document.revision:
                raise SourceUnavailable("taskgraph_execution_revision_changed")
            historical = structure.network
            for name in ("mission_id", "plan_revision", "occurrences", "method_instances", "adopted_instance_ids",
                         "root_occurrence_ids", "order_constraints", "data_requirements", "typed_edges",
                         "obligation_coverage", "required_obligations"):
                if getattr(view.network, name) != getattr(historical, name):
                    raise SourceUnavailable("taskgraph_execution_structure_changed", detail=name)
            if view.accepted is None:
                raise SourceUnavailable("taskgraph_accepted_outputs_missing")
            semantics = HtnStore(self.store)
            bindings = tuple(sorted(view.network.task_bindings, key=lambda item: str(item.task_id)))
            expected_tasks = {str(item.task_id) for item in historical.occurrences}
            if {str(item.task_id) for item in bindings} != expected_tasks:
                raise SourceUnavailable("taskgraph_binding_set_incomplete")
            for binding in bindings:
                if semantics.task_semantics_of(mission_id, str(binding.task_id)) != binding:
                    # The hierarchy decorates adopted method identity on its view;
                    # compare that field to the adopted draft, not raw current bytes.
                    from dataclasses import replace
                    actual = semantics.task_semantics_of(mission_id, str(binding.task_id))
                    if actual is None or replace(binding, adopted_method_instance_id=actual.adopted_method_instance_id) != actual:
                        raise SourceUnavailable("taskgraph_binding_currentness_failed")
            methods = semantics.list_methods()
            available = {method.contract.method_ref() for method in methods}
            if any(draft.method_ref not in available for draft in view.network.method_instances):
                raise SourceUnavailable("taskgraph_method_registry_incomplete")
            epochs = tuple(sorted(dispatcher.scope_epochs(mission_id).items()))
            if epochs != structure.token.validity_epochs:
                raise SourceUnavailable("taskgraph_epoch_source_changed")
            capabilities = world.capabilities()
            if not isinstance(capabilities, CapabilitySnapshot):
                raise SourceUnavailable("taskgraph_capabilities_source_invalid")
            # Capability booleans are the installed deployment's declaration only.
            # This reader does not promote default axes into a physical health probe.
            policy = dispatcher.resolution_policy_for(mission_id, now_ms=now_ms)
            if not isinstance(policy, ResolutionPolicy):
                raise SourceUnavailable("taskgraph_input_policy_source_invalid")
            rules = {"policy": dispatcher.target_rules_policy(),
                     "tasks": {task_id: dispatcher.target_rules_for(task_id)
                               for task_id in sorted(expected_tasks)}}
            duties = ObligationStore(self.store)
            obligations = duties.list_obligations(mission_id)
            accounts = tuple(duties.account(mission_id, item.obligation_id) for item in obligations)
            budget_ids = tuple(str(row[0]) for row in db.execute(
                "SELECT account_id FROM budget_accounts WHERE mission_id=? ORDER BY account_id", (mission_id,)))
            # Native HTN roots exist as semantic compounds before a legacy Task
            # or its account is materialized. Planning uses the Mission account;
            # require Task accounts only for actual original Task rows. Missing
            # primitive Tasks are already rejected by occurrence_outcomes above.
            materialized_tasks = {task.id for task in self.store.list_tasks(mission_id)}
            required_budgets = {"budget:" + mission_id,
                                *("budget:" + task for task in expected_tasks & materialized_tasks)}
            if not required_budgets <= set(budget_ids):
                raise SourceUnavailable("taskgraph_budget_source_incomplete")
            budgets = tuple(dispatcher.commit.ledger.account(identity) for identity in budget_ids)
            return LocalExecutionSources(structure=structure, view=view,
                current_task_bindings=source("bindings", bindings), current_method_registrations=source("methods", methods),
                witnesses=source("witnesses", WitnessSources(witnesses=view.witnesses, starts=view.starts, licences=view.licences)),
                scope_epochs=source("epochs", epochs), accepted_outputs=source("accepted_outputs", view.accepted),
                source_input_policy=source("input_policy", policy),
                target_rules=source("target_rules", rules),
                obligations=source("obligations", obligations), obligation_accounts=source("demand", accounts),
                active_demands=source("active_demands", structure.pins.demand_refs),
                independent_obligations=source("independent_obligations", read_independent_demands(self.store, mission_id, accounts)),
                budget_snapshots=source("budget", budgets), deployment_capabilities=source("capabilities", capabilities),
                local_work=source("local_work", read_local_work(self.store, mission_id)))
