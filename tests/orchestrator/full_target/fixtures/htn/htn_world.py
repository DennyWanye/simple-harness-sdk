# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Builders shared by the FULL-TARGET-1.4 P2.1 tests.

A test says what the world knows and which task types exist; the plumbing — the
evidence entries behind a truth value, the derived content hashes, the capability
table — is built here so a test that asks for FALSE gets a FALSE backed by a real
counter-observation rather than by a convenient constructor argument.

Two builders matter:

:class:`Env`
    a whole deployment: schemas, predicates, task types, capabilities, a method
    registry and the admission policy they are decided against.  :func:`seed_env`
    fills one from the shipped seed library; :meth:`Env.register_type` and
    :meth:`Env.admit` let a test add its own declarations with no seed data at all.
:func:`root_network`
    the one-occurrence starting network for a compound goal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_orchestrator.contracts.evidence_state import (
    EvidenceEntry,
    EvidenceSnapshot,
    SupportCount,
    TruthValue,
)
from agent_orchestrator.contracts.htn import (
    CriterionLink,
    GoalSignature,
    MethodComposition,
    MethodContract,
    MethodOrdering,
    MethodStep,
    ObligationRelation,
    PortSpec,
    RegistryAuthor,
    ResourceRef,
    ReusePolicy,
    SideEffectKind,
    TaskForm,
    TaskSemanticBindingV1,
    parse_condition,
    parse_conditions,
)
from agent_orchestrator.contracts.obligations import Obligation, ObligationLedger
from agent_orchestrator.contracts.semantic_base import (
    TypedRef,
    TypedRefKind,
    VersionedRef,
    content_hash_of,
)
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET, TaskNetworkSnapshot
from agent_orchestrator.knowledge.predicates import (
    ArgumentType,
    PredicateParameter,
    PredicateRegistry,
    PredicateSignature,
    WorldAssumption,
    proposition_key,
)
from agent_orchestrator.planning.htn.applicability import CapabilityRecord, CapabilitySnapshot
from agent_orchestrator.planning.htn.compiler import RootNetwork
from agent_orchestrator.planning.htn.registry import (
    AdmissionPolicy,
    AdmissionReceipt,
    MethodProposal,
    MethodRegistry,
    ObjectSchema,
    SchemaCatalog,
    SchemaField,
    TaskTypeCatalog,
    TaskTypeSpec,
)
from agent_orchestrator.planning.htn.seed_methods import (
    SEED_ROOT,
    SeedDomain,
    admit_domain,
    available_domains,
    install_domain,
    load_domain_path,
    seed_content_hash,
)

FIXTURE_ROOT = Path(__file__).resolve().parent
PROPOSAL_ROOT = FIXTURE_ROOT / "proposals"
DOMAIN_ROOT = FIXTURE_ROOT / "domains"

MISSION = "mission-1"


def ref(identifier: str, version: int = 1) -> VersionedRef:
    """A reference hashed the way the seed loader hashes one."""

    return VersionedRef(
        id=identifier, version=version, content_hash=seed_content_hash(identifier, version)
    )


def acceptance_ref(identifier: str) -> TypedRef:
    """A typed Acceptance reference; ``ChildBinding`` refuses a bare id."""

    return TypedRef(
        kind=TypedRefKind.ACCEPTANCE,
        id=identifier,
        revision=1,
        content_hash=content_hash_of(identifier),
    )


def observation_ref(key: str) -> TypedRef:
    return TypedRef(
        kind=TypedRefKind.OBSERVATION,
        id=f"obs-{content_hash_of(key)[:24]}",
        revision=1,
        content_hash=content_hash_of(key),
    )


def evidence_entry(key: str, truth: TruthValue) -> EvidenceEntry | None:
    """The evidence that *produces* ``truth``.  UNKNOWN is the absence of any."""

    if truth is TruthValue.UNKNOWN:
        return None
    if truth is TruthValue.TRUE:
        support, authoritative = SupportCount(1, 0), False
    elif truth is TruthValue.FALSE:
        support, authoritative = SupportCount(0, 1), True
    else:
        support, authoritative = SupportCount(1, 1), True
    return EvidenceEntry(
        proposition_key=key,
        support=support,
        observation_refs=(observation_ref(key),),
        authoritative_negative=authoritative,
    )


@dataclass
class Env:
    """One deployment's declarations and its method registry."""

    mission: str = MISSION
    schemas: SchemaCatalog = field(default_factory=SchemaCatalog)
    predicates: PredicateRegistry = field(default_factory=PredicateRegistry)
    catalog: TaskTypeCatalog = field(default_factory=TaskTypeCatalog)
    registry: MethodRegistry = field(default_factory=MethodRegistry)
    capability_ids: list[str] = field(default_factory=list)
    domains: tuple[SeedDomain, ...] = ()
    #: The real ``DeploymentPlanningWorld`` this Env was filled from, when it was
    #: built by :func:`seed_env`.  A hand-built Env has none: it declares its own
    #: types with no seed data at all, which is the case the fixture exists for.
    world: Any = None
    _entries: dict[str, EvidenceEntry] = field(default_factory=dict)

    # -- declarations -------------------------------------------------------------

    def register_schema(
        self, identifier: str, fields: Sequence[tuple[str, str]] = ()
    ) -> ObjectSchema:
        schema = ObjectSchema(
            schema_ref=ref(identifier),
            fields=tuple(SchemaField(name=name, type=ArgumentType(kind)) for name, kind in fields),
        )
        self.schemas.register(schema)
        return schema

    def register_predicate(
        self,
        identifier: str,
        parameters: Sequence[tuple[str, str]] = (("subject", "string"),),
        *,
        closed: bool = False,
        observers: Sequence[str] = ("observer-1",),
    ) -> PredicateSignature:
        signature = PredicateSignature(
            predicate_ref=ref(identifier),
            parameters=tuple(
                PredicateParameter(name=name, type=ArgumentType(kind)) for name, kind in parameters
            ),
            world_assumption=WorldAssumption.CLOSED if closed else WorldAssumption.OPEN,
            observer_ids=tuple(observers),
            statement=f"{identifier} holds",
        )
        self.predicates.register(signature)
        return signature

    def register_capability(self, *capability_ids: str) -> None:
        for item in capability_ids:
            if item not in self.capability_ids:
                self.capability_ids.append(item)

    def register_type(
        self,
        identifier: str,
        *,
        form: TaskForm = TaskForm.PRIMITIVE,
        parameters: Sequence[tuple[str, str]] = (),
        criteria: Sequence[str] = (),
        inputs: Sequence[tuple[str, str, bool]] = (),
        outputs: Sequence[tuple[str, str]] = (),
        capabilities: Sequence[str] = (),
        effect: SideEffectKind = SideEffectKind.EXTERNAL_READ,
        reversible: bool = True,
        reuse: ReusePolicy = ReusePolicy.NEW_WORK,
        observes: Sequence[str] = (),
        preconditions: Sequence[Mapping[str, Any]] = (),
        reads: Sequence[tuple[str, str]] = (),
        writes: Sequence[tuple[str, str]] = (),
        effect_identity: str | None = None,
        domain: str | None = None,
        set_port: bool = False,
    ) -> TaskTypeSpec:
        parameter_schema = f"{identifier}.params"
        self.register_schema(parameter_schema, parameters)
        self.register_schema(f"{identifier}.outputs")
        for _, schema_id, *_ in inputs:
            self.register_schema(schema_id)
        for _, schema_id in outputs:
            self.register_schema(schema_id)
        self.register_capability(*capabilities)
        spec = TaskTypeSpec(
            task_type_ref=ref(identifier),
            form=form,
            goal_signature=GoalSignature(
                signature_id=identifier,
                version=1,
                parameter_schema_ref=ref(parameter_schema),
                output_schema_ref=ref(f"{identifier}.outputs"),
                statement=f"goal {identifier}",
                coverage_criteria=tuple(criteria),
            ),
            input_ports=tuple(
                PortSpec(
                    port_key=key,
                    schema_ref=ref(schema_id),
                    required=required,
                    cardinality="set" if set_port else "single",
                )
                for key, schema_id, required in inputs
            ),
            output_ports=tuple(
                PortSpec(port_key=key, schema_ref=ref(schema_id)) for key, schema_id in outputs
            ),
            parameter_schema_ref=ref(parameter_schema),
            output_schema_ref=ref(f"{identifier}.outputs"),
            operator_ref=ref(f"{identifier}.operator") if form is TaskForm.PRIMITIVE else None,
            required_capabilities=tuple(capabilities),
            side_effect_kind=effect if form is TaskForm.PRIMITIVE else SideEffectKind.NONE,
            reversible=reversible,
            resource_reads=tuple(ResourceRef(namespace=n, object_id=o) for n, o in reads),
            resource_writes=tuple(ResourceRef(namespace=n, object_id=o) for n, o in writes),
            reuse_policy=reuse,
            observes=tuple(ref(item) for item in observes),
            preconditions=parse_conditions(list(preconditions), "preconditions"),
            effect_identity=effect_identity,
            domain=domain,
        )
        self.catalog.register(spec)
        return spec

    # -- evidence -----------------------------------------------------------------

    def say(self, predicate: str, arguments: Mapping[str, Any], truth: TruthValue) -> str:
        """Record the evidence that makes one proposition have ``truth``."""

        signature = self.predicates.require(ref(predicate))
        key = proposition_key(signature, dict(arguments))
        entry = evidence_entry(key, truth)
        if entry is None:
            self._entries.pop(key, None)
        else:
            self._entries[key] = entry
        return key

    def snapshot(self, snapshot_id: str = "snapshot-1", as_of_ms: int = 1_000) -> EvidenceSnapshot:
        return EvidenceSnapshot(
            snapshot_id=snapshot_id,
            as_of_ms=as_of_ms,
            scope_id=self.mission,
            scope_epoch=1,
            support_revision=1,
            entries=tuple(self._entries[key] for key in sorted(self._entries)),
        )

    def capabilities(self, *, unavailable: Sequence[str] = ()) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            records=tuple(
                CapabilityRecord(capability_id=item, healthy=item not in unavailable)
                for item in sorted(self.capability_ids)
            )
        )

    # -- policy and admission -----------------------------------------------------

    def policy(
        self,
        *,
        mission: str | None = None,
        # The deployment assembly asks for a policy by ``mission_id``; accepting the
        # spelling here keeps a hand-built Env usable wherever a real
        # ``DeploymentPlanningWorld`` is.
        mission_id: str | None = None,
        unavailable: Sequence[str] = (),
        known_capabilities: Sequence[str] | None = None,
        **overrides: Any,
    ) -> AdmissionPolicy:
        records = (
            tuple(CapabilityRecord(capability_id=item) for item in sorted(known_capabilities))
            if known_capabilities is not None
            else self.capabilities(unavailable=unavailable).records
        )
        return AdmissionPolicy(
            policy_ref="policy-1",
            policy_version=1,
            mission_id=mission or mission_id or self.mission,
            predicates=self.predicates,
            task_types=self.catalog,
            schemas=self.schemas,
            capabilities=CapabilitySnapshot(records=records),
            **overrides,
        )

    def admit(
        self,
        method: MethodContract,
        *,
        author: RegistryAuthor = RegistryAuthor.SYSTEM,
        declared_status: Any = None,
        policy: AdmissionPolicy | None = None,
    ) -> AdmissionReceipt:
        proposal = (
            MethodProposal(method=method, author=author)
            if declared_status is None
            else MethodProposal(method=method, author=author, declared_status=declared_status)
        )
        return self.registry.admit(proposal, author=author, policy=policy or self.policy())


def seed_env(mission: str = MISSION, *, root: Path | None = None) -> Env:
    """An :class:`Env` with the shipped seed domains installed and admitted.

    It **delegates** to the deployment assembly (P2.3c part 2b) rather than keeping
    a second copy of "how a seed library becomes a PlanningWorld".  Before that
    assembly existed the fixture was the only thing that knew, which is exactly why
    a real deployment could not build one.  What the fixture still owns is the
    *test* conveniences on top: ``register_type``, ``say``, ``admit``.

    The capability table is forced to "every declared capability is available",
    which is the fixture's own claim and not the assembly's: these suites decide
    applicability from evidence, and a host without ``code_test`` deployed would
    otherwise silently change every seed-domain expectation.  A test that wants the
    derived table asks :func:`seed_world` for it.
    """

    world = seed_world(mission, root=root)
    env = Env(
        mission=mission,
        schemas=world.schemas,
        predicates=world.predicates,
        catalog=world.catalog,
        registry=world.registry,
        capability_ids=list(world.capability_ids),
        domains=world.domains,
    )
    env.world = world
    return env


def seed_world(mission: str = MISSION, *, root: Path | None = None, **overrides: Any):
    """The real :class:`DeploymentPlanningWorld` for the shipped seed domains."""

    from agent_orchestrator.planning.htn.world import build_planning_world

    directory = SEED_ROOT if root is None else Path(root)
    fields: dict[str, Any] = {
        "domains": available_domains(directory),
        "root": directory,
        "deployed_layers": ("code_test",),
    }
    fields.update(overrides)
    return build_planning_world(mission, **fields)


def add_domain(env: Env, path: Path) -> SeedDomain:
    """Install and admit an extra domain from anywhere on disk."""

    domain = load_domain_path(path)
    install_domain(domain, schemas=env.schemas, predicates=env.predicates, catalog=env.catalog)
    env.register_capability(*domain.capability_ids)
    admit_domain(domain, registry=env.registry, policy=env.policy())
    env.domains = (*env.domains, domain)
    return domain


# --------------------------------------------------------------------------------------
# Method construction
# --------------------------------------------------------------------------------------


def atom(predicate: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "op": "predicate",
        "predicate_ref": ref(predicate).to_json(),
        "arguments": dict(arguments),
    }


def param(name: str) -> dict[str, Any]:
    return {"op": "parameter", "name": name}


def const(value: Any) -> dict[str, Any]:
    return {"op": "constant", "value": value}


def out(step: str, port: str) -> dict[str, Any]:
    return {"op": "output", "step": step, "port": port}


def step(
    local_id: str,
    task_type: str,
    form: TaskForm,
    arguments: Mapping[str, Any] | None = None,
    *,
    capabilities: Sequence[str] = (),
    relation: ObligationRelation = ObligationRelation.REFINES_PARENT,
    reuse_policy: ReusePolicy | None = None,
) -> MethodStep:
    from agent_orchestrator.contracts.htn import StructureBudget, parse_arguments

    return MethodStep(
        local_id=local_id,
        task_type_ref=ref(task_type),
        form=form,
        arguments=parse_arguments(dict(arguments or {}), f"step.{local_id}", StructureBudget(512)),
        required_capabilities=tuple(capabilities),
        obligation_relation=relation,
        reuse_policy=reuse_policy,
    )


def method(
    method_id: str,
    goal_type: str,
    *,
    parameter_schema: str,
    applicable: Sequence[Mapping[str, Any]] = (),
    steps: Sequence[MethodStep] = (),
    ordering: Sequence[tuple[str, str]] = (),
    links: Sequence[tuple[str, str | None, str]] = (),
    finalizer: str | None = None,
    version: int = 1,
    output_schema: str | None = None,
) -> MethodContract:
    return MethodContract(
        method_id=method_id,
        method_version=version,
        goal_type_ref=ref(goal_type),
        parameter_schema_ref=ref(parameter_schema),
        output_schema_ref=ref(output_schema or f"{goal_type}.outputs"),
        applicable_when=tuple(parse_condition(item) for item in applicable),
        exploration_assumptions=(),
        steps=tuple(steps),
        ordering=tuple(MethodOrdering(before=before, after=after) for before, after in ordering),
        required_capabilities=(),
        expected_effects=(),
        composition=MethodComposition(
            criterion_links=tuple(
                CriterionLink(
                    parent_criterion_id=parent,
                    child_step=child,
                    child_criterion_id=criterion,
                    evidence_requirement=f"{parent} is covered by {child or 'the composition'}",
                )
                for parent, child, criterion in links
            ),
            outputs={},
            finalizer_step=finalizer,
            independent_review_required=True,
        ),
        basis_refs=(),
    )


# --------------------------------------------------------------------------------------
# Networks and ledgers
# --------------------------------------------------------------------------------------


def task_binding(
    env: Env,
    task_type: str,
    *,
    task_id: str = "task-root",
    obligation: str = "obl-root",
    parameters: Mapping[str, Any] | None = None,
    scope: str | None = None,
) -> TaskSemanticBindingV1:
    spec = env.catalog.require(ref(task_type))
    return TaskSemanticBindingV1(
        task_id=task_id,
        obligation_id=obligation,
        contract_revision=1,
        contract_hash=content_hash_of([task_id, task_type]),
        form=spec.form,
        goal_signature=spec.goal_signature,
        typed_parameters=dict(parameters or {}),
        requirement_refs=("req-1",),
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        operator_ref=spec.operator_ref,
        semantic_scope=scope or env.mission,
        capability_requirements=spec.required_capabilities,
    )


def root_network(
    env: Env,
    binding: TaskSemanticBindingV1,
    *,
    extra_occurrences: Sequence[Any] = (),
    extra_bindings: Sequence[TaskSemanticBindingV1] = (),
) -> TaskNetworkSnapshot:
    return RootNetwork(
        mission_id=env.mission,
        root_task=binding,
        extra_occurrences=tuple(extra_occurrences),
        extra_bindings=tuple(extra_bindings),
    ).snapshot()


def ledger_for(
    binding: TaskSemanticBindingV1, *, fuel: int = 3, mission: str = MISSION
) -> ObligationLedger:
    ledger = ObligationLedger(default_fuel=fuel)
    ledger.register(
        Obligation(
            obligation_id=binding.obligation_id,
            mission_id=mission,
            requirement_refs=("req-1",),
            goal_signature_id=binding.goal_signature.signature_id,
        )
    )
    return ledger


def load_proposal(name: str) -> dict[str, Any]:
    """A scripted ``method_proposal`` block, as a model would emit it (§18.5 C8)."""

    from agent_orchestrator.planning.htn.seed_methods.loader import fill_content_hashes

    payload = json.loads((PROPOSAL_ROOT / f"{name}.json").read_text(encoding="utf-8"))
    return fill_content_hashes(payload)


BUDGET = DEFAULT_PROJECTION_BUDGET


__all__ = (
    "BUDGET",
    "DOMAIN_ROOT",
    "FIXTURE_ROOT",
    "MISSION",
    "PROPOSAL_ROOT",
    "Env",
    "acceptance_ref",
    "add_domain",
    "atom",
    "const",
    "evidence_entry",
    "ledger_for",
    "load_proposal",
    "method",
    "observation_ref",
    "out",
    "param",
    "ref",
    "root_network",
    "seed_env",
    "seed_world",
    "step",
    "task_binding",
)
