# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2b: the deployment-side ``PlanningWorld``.

P2.3b's deviation 4 and P2.3c part 2's §10 said the same thing twice: the seed
library could be *loaded* (``seed_methods.loader``) but nothing in ``src`` ever
assembled it into the object
:meth:`~...orchestrator.event_handler.EventHandler.install_hierarchical` wants —
so every hierarchical Mission outside the test suite failed at
``require_planning_world``, and a real-model smoke run could not even start.  This
module is that assembly.

What the structural type asks for and where each half comes from:

``catalog`` / ``schemas`` / ``registry`` / ``predicates``
    the four declaration stores, filled by ``install_library`` and ``admit_domain``.
    Methods go through the admission protocol rather than being registered
    directly, because §7.3's bypass is closed for seed data too.
``capabilities()``
    **not** a list in this file.  It is derived from what this deployment actually
    has: a capability is ``registered`` only when some registered *primitive* task
    type with a real ``operator_ref`` declares it, and ``configured`` only when the
    verification layer it needs is in ``deployed_layers``.  A capability a domain
    mentions and nothing implements is therefore reported unavailable *with a
    reason*, which is what the applicability report and the Planner package read.
``snapshot()``
    the evidence, read from ``htn_store``'s ``observations`` table every call.  A
    cached snapshot is the stale read ADR-13 spends a gate refusing, and the whole
    reason a refinement round can change its mind is that the round after an
    observation sees the observation.

One thing this module adds on top of the structural type, because a deployment
needs it and nothing else owns it: the **observer index** (``observation_pipeline``)
belongs to the same assembly as the predicate registry it is checked against.  A
world built without observers answers ``OBSERVER_UNAVAILABLE`` for every predicate,
which is the honest answer for a deployment that installed no reader — never FALSE.

Assigning observers is its own small problem, solved here and not in
``build_index``: the shipped ``code`` pack deliberately ships *two* identities for
``code.working-tree-clean`` (§6.6 C28 allows a CLOSED predicate several authorised
observers), while an index may hold only one reader per predicate.  Who reads what
is a deployment decision, so :func:`assign_readers` makes it one — first observer in
the given order wins the predicate, and the order is the deployment's precedence
declaration rather than an accident of the tuple ``code_observers`` returns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...contracts.evidence_state import EvidenceEntry, EvidenceSnapshot
from ...contracts.models import ContractError
from ...contracts.semantic_base import VersionedRef, content_hash_of
from ...knowledge.predicates import PredicateRegistry
from .applicability import CapabilityRecord, CapabilitySnapshot
from .observation_pipeline import ObserverIndex, build_index
from .observers import PredicateObserver
from .registry import (
    AdmissionPolicy,
    MethodRegistry,
    SchemaCatalog,
    TaskForm,
    TaskTypeCatalog,
    TaskTypeSpec,
)
from .seed_methods import SEED_ROOT, SeedDomain, admit_domain, available_domains
from .seed_methods.loader import install_library

#: Which deployed verification layer a capability needs before it counts as
#: *configured*.  This is a **deployment** fact, not a domain fact: the seed data
#: says a task type needs ``tests.run``, and it is this host that knows running a
#: test suite means the ``code_test`` layer is deployed.  Keeping it here rather
#: than in ``seed_methods/`` is why a third domain still needs data only.
#:
#: The table is the deployment's **declaration of what it can run**, so it lists
#: every capability the shipped domains name, and ``None`` is a positive statement
#: ("this host needs no extra layer for it"), not an absence.  A capability that is
#: not listed at all is reported ``configured=False``: review P1-6 found the old
#: rule (``needed is None or needed in layers``) fail-*open*, so any capability the
#: deployment had never heard of was reported as configured and a method could be
#: admitted against a tool this host does not have.  Adding a domain now means
#: adding its capabilities here, which is the point.
CAPABILITY_LAYERS: Mapping[str, str | None] = {
    # code
    "repo.read": None,
    "repo.write": None,
    "tests.run": "code_test",
    # appworld
    "appworld.api": None,
    "appworld.read": None,
    "appworld.search": None,
    "appworld.write": None,
}


#: The scope every snapshot is cut in when a caller does not say otherwise.
DEFAULT_SCOPE = "mission"


# --------------------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------------------


def declared_capability_ids(catalog: TaskTypeCatalog) -> tuple[str, ...]:
    """Every capability the registered task types mention, in a stable order."""

    out: list[str] = []
    for spec in catalog.task_types():
        out.extend(str(item) for item in spec.required_capabilities)
    return tuple(sorted(dict.fromkeys(out)))


def _implemented(catalog: TaskTypeCatalog, capability_id: str) -> bool:
    """Is there a registered primitive type with a real operator behind this id?

    "Registered" in §14.2 means the deployment *has* the thing, and what it has for
    a capability is an operator it can dispatch to.  A capability named only by a
    compound type is a capability nobody can execute, and reporting it as registered
    would let a method be admitted against a tool that does not exist.
    """

    for spec in catalog.task_types():
        if capability_id not in {str(item) for item in spec.required_capabilities}:
            continue
        if spec.form is TaskForm.PRIMITIVE and spec.operator_ref is not None:
            return True
    return False


def capability_records(
    catalog: TaskTypeCatalog,
    *,
    deployed_layers: Iterable[str] = (),
    capability_layers: Mapping[str, str | None] = CAPABILITY_LAYERS,
    unhealthy: Iterable[str] = (),
    unauthorized: Iterable[str] = (),
) -> tuple[CapabilityRecord, ...]:
    """The deployment's capability table, derived rather than declared.

    Each of §14.2's axes is answered by a different fact, so a Planner told "this
    is unavailable" can also be told *why*: nothing implements it, the layer it
    needs is not deployed, the tool is down, or this Mission may not use it.
    """

    layers = frozenset(str(item) for item in deployed_layers)
    down = frozenset(str(item) for item in unhealthy)
    denied = frozenset(str(item) for item in unauthorized)
    records: list[CapabilityRecord] = []
    for capability_id in declared_capability_ids(catalog):
        if capability_id in capability_layers:
            needed = capability_layers[capability_id]
            configured = needed is None or needed in layers
        else:
            # Fail closed (review P1-6): an undeclared capability is one this host
            # cannot promise, and promising it is how a method gets admitted
            # against a tool nobody deployed.
            configured = False
        records.append(
            CapabilityRecord(
                capability_id=capability_id,
                registered=_implemented(catalog, capability_id),
                configured=configured,
                healthy=capability_id not in down,
                authorized=capability_id not in denied,
            )
        )
    return tuple(records)


# --------------------------------------------------------------------------------------
# observers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AssignedReader:
    """One observer, narrowed to the predicates this deployment routes to it.

    A thin delegation rather than a subclass: ``observe`` is the observer's own and
    is not touched, so the reading is the reader's and only the *routing* is the
    deployment's.
    """

    reader: PredicateObserver
    predicates: tuple[str, ...]

    @property
    def observer_id(self) -> str:
        return self.reader.observer_id

    def predicate_ids(self) -> tuple[str, ...]:
        return self.predicates

    def observe(self, signature: Any, arguments: Mapping[str, Any], *, now_ms: int) -> Any:
        return self.reader.observe(signature, arguments, now_ms=now_ms)


def assign_readers(observers: Sequence[PredicateObserver]) -> tuple[PredicateObserver, ...]:
    """One reader per predicate, chosen by the order the deployment listed them.

    §6.6 C28 lets a CLOSED predicate list several authorised observers, and the
    shipped ``code`` pack uses that: ``code.repo-observer`` and
    ``code.workspace-observer`` are both allowed to deny
    ``code.working-tree-clean``.  An *index* may still hold only one, because two
    readers of one proposition is two answers with no rule saying which wins — so
    the deployment picks, by order, and an observer left with nothing to read is
    dropped rather than indexed under an empty assignment.
    """

    taken: set[str] = set()
    out: list[PredicateObserver] = []
    for observer in observers:
        assigned = tuple(
            name for name in (str(item) for item in observer.predicate_ids()) if name not in taken
        )
        taken.update(assigned)
        if not assigned:
            continue
        if assigned == tuple(str(item) for item in observer.predicate_ids()):
            out.append(observer)
        else:
            out.append(_AssignedReader(reader=observer, predicates=assigned))
    return tuple(out)


def domain_observers(
    domains: Sequence[str],
    *,
    worktree: Path | str | None = None,
    appworld_episode: Any = None,
    appworld_scope: str = "appworld-episode",
) -> tuple[PredicateObserver, ...]:
    """The observers the named domains can be read with on *this* host.

    A domain whose reader needs something this deployment did not supply — ``code``
    without a worktree — contributes no observer at all.  That is deliberate: an
    observer pointed at a directory that is not there would answer
    ``code.repo-checked-out`` FALSE about a repository nobody named, and "this
    deployment cannot read it" is a different fact from "it is not checked out".
    """

    out: list[PredicateObserver] = []
    for name in domains:
        if name == "code" and worktree is not None:
            from .observers.code import code_observers

            out.extend(code_observers(Path(worktree)))
        elif name == "appworld" and appworld_episode is not None:
            from .observers.appworld import appworld_observers

            out.extend(appworld_observers(appworld_episode, scope_id=appworld_scope))
    return tuple(out)


# --------------------------------------------------------------------------------------
# the world
# --------------------------------------------------------------------------------------


@dataclass
class DeploymentPlanningWorld:
    """One deployment's declarations, capability table and evidence read.

    Satisfies the ``PlanningWorld`` structural type
    (:mod:`~...orchestrator.hierarchical_dispatch`): ``catalog`` / ``schemas`` /
    ``registry`` / ``predicates`` are attributes, ``snapshot()`` and
    ``capabilities()`` are calls.  Nothing here caches: both calls read the store
    or the record table every time they are made.
    """

    mission_id: str
    schemas: SchemaCatalog
    predicates: PredicateRegistry
    catalog: TaskTypeCatalog
    registry: MethodRegistry
    records: tuple[CapabilityRecord, ...] = ()
    observers: ObserverIndex | None = None
    domains: tuple[SeedDomain, ...] = ()
    semantics: Any = None
    scope_id: str = DEFAULT_SCOPE
    policy_ref: str = "deployment-policy"
    policy_version: int = 1
    max_steps: int = 64
    _empty_at_ms: int = field(default=0, repr=False)

    # -- the structural type -------------------------------------------------------
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(records=self.records)

    def snapshot(self) -> EvidenceSnapshot:
        """The evidence this Mission has recorded, as a planning snapshot.

        Built from the ``observations`` table, one entry per proposition key, with
        the support counts merged by :meth:`EvidenceEntry.from_observations` — so a
        counter-observation is not outvoted and an authoritative negative keeps its
        flag.  A proposition with no observation is simply absent, which the
        snapshot's own contract reads as UNKNOWN.

        ``support_revision`` is the number of observations behind the snapshot.  It
        moves exactly when the evidence moves, which is what a read-set needs from
        it: a decision made at revision *n* is refused if the world has since
        learned an *n+1*-th fact.
        """

        if self.semantics is None:
            return EvidenceSnapshot(
                snapshot_id="snapshot-empty",
                as_of_ms=int(self._empty_at_ms),
                scope_id=self.scope_id,
                scope_epoch=0,
                support_revision=0,
            )
        observations = tuple(self.semantics.list_observations(self.mission_id))
        grouped: dict[str, list[Any]] = {}
        for observation in observations:
            grouped.setdefault(str(observation.proposition_key), []).append(observation)
        entries = tuple(
            EvidenceEntry.from_observations(key, tuple(grouped[key])) for key in sorted(grouped)
        )
        as_of = max((int(item.recorded_at_ms) for item in observations), default=0)
        return EvidenceSnapshot(
            snapshot_id="snap-"
            + content_hash_of({"entries": [entry.to_json() for entry in entries]})[:24],
            as_of_ms=as_of,
            scope_id=self.scope_id,
            scope_epoch=int(self.semantics.epoch(self.mission_id, self.scope_id)),
            support_revision=len(observations),
            entries=entries,
        )

    # -- what a deployment does with it --------------------------------------------
    def policy(self, **overrides: Any) -> AdmissionPolicy:
        """The admission policy this deployment decides a method proposal against."""

        fields: dict[str, Any] = {
            "policy_ref": self.policy_ref,
            "policy_version": self.policy_version,
            "mission_id": self.mission_id,
            "predicates": self.predicates,
            "task_types": self.catalog,
            "schemas": self.schemas,
            "capabilities": self.capabilities(),
            "max_steps": self.max_steps,
        }
        fields.update(overrides)
        return AdmissionPolicy(**fields)

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(str(item.capability_id) for item in self.records)

    @property
    def observer_index(self) -> ObserverIndex:
        """The evidence readers, or an empty index — never ``None`` for a caller.

        A deployment that installed no observer still has a well-defined answer for
        "who reads this predicate": nobody, which
        :func:`~.observation_pipeline.observe_predicate` turns into
        ``OBSERVER_UNAVAILABLE``.  Handing back ``None`` would make every call site
        invent that branch for itself.
        """

        if self.observers is None:
            return build_index(self.predicates, ())
        return self.observers

    def observed_predicate_ids(self) -> tuple[str, ...]:
        return self.observer_index.predicate_ids()

    def type_for(self, reference: VersionedRef) -> TaskTypeSpec | None:
        return self.catalog.resolve(reference)


def build_planning_world(
    mission_id: str,
    *,
    domains: Sequence[str] = ("code",),
    root: Path | str = SEED_ROOT,
    semantics: Any = None,
    worktree: Path | str | None = None,
    appworld_episode: Any = None,
    deployed_layers: Iterable[str] = (),
    unhealthy: Iterable[str] = (),
    unauthorized: Iterable[str] = (),
    observers: Sequence[PredicateObserver] | None = None,
    scope_id: str = DEFAULT_SCOPE,
    capability_layers: Mapping[str, str | None] = CAPABILITY_LAYERS,
) -> DeploymentPlanningWorld:
    """Assemble one deployment's ``PlanningWorld`` from the seed library.

    The order is the protocol's: install the declarations first, derive the
    capability table from what was installed, and only then admit the methods —
    because §7.3 decides a method against the capabilities the deployment actually
    has, and admitting first would decide it against nothing.

    ``domains`` names what to install; an unknown name is refused here rather than
    silently producing a world with no methods, since "the Planner was offered no
    method" and "the domain was misspelt in the deployment config" repair
    differently.
    """

    root_path = Path(root)
    known = set(available_domains(root_path))
    chosen = tuple(str(item) for item in domains)
    unknown = [name for name in chosen if name not in known]
    if unknown:
        raise ContractError(
            f"no seed domain named {unknown} under {root_path}; this deployment installs "
            f"{sorted(known)}"
        )
    schemas = SchemaCatalog()
    predicates = PredicateRegistry()
    catalog = TaskTypeCatalog()
    registry = MethodRegistry()
    installed = install_library(
        chosen, root=root_path, schemas=schemas, predicates=predicates, catalog=catalog
    )
    records = capability_records(
        catalog,
        deployed_layers=deployed_layers,
        capability_layers=capability_layers,
        unhealthy=unhealthy,
        unauthorized=unauthorized,
    )
    world = DeploymentPlanningWorld(
        mission_id=mission_id,
        schemas=schemas,
        predicates=predicates,
        catalog=catalog,
        registry=registry,
        records=records,
        domains=installed,
        semantics=semantics,
        scope_id=scope_id,
    )
    chosen_observers = (
        domain_observers(chosen, worktree=worktree, appworld_episode=appworld_episode)
        if observers is None
        else tuple(observers)
    )
    world.observers = build_index(predicates, assign_readers(chosen_observers))
    policy = world.policy()
    for domain in installed:
        admit_domain(domain, registry=registry, policy=policy)
    publish_methods(world)
    return world


def publish_methods(world: DeploymentPlanningWorld) -> tuple[str, ...]:
    """Put every admitted method into the **library**, not only into memory.

    The in-memory :class:`MethodRegistry` is what the admission protocol decides
    against; ``compile_proposal`` then reads the chosen method back out of
    ``htn_store`` — because the definition a plan revision was compiled from has to
    be durable and re-readable at exactly the version the commit recorded.  Before
    this, a deployment assembled its world, the Planner proposed a perfectly valid
    refinement against a method the registry held, and the compile died with
    "method … is not stored".

    A method already stored with the same bytes is left alone (the store's own
    re-registration rule), so a second assembly over the same library is a no-op
    rather than a conflict.
    """

    if world.semantics is None:
        return ()
    published: list[str] = []
    for reference in world.registry.method_refs():
        contract = world.registry.definition(reference)
        registration = world.registry.registration(reference)
        if contract is None or registration is None:
            continue
        world.semantics.register_method(contract, registration)
        published.append(f"{reference.method_id}@{int(reference.version)}")
    return tuple(published)


__all__ = (
    "CAPABILITY_LAYERS",
    "DEFAULT_SCOPE",
    "DeploymentPlanningWorld",
    "assign_readers",
    "build_planning_world",
    "capability_records",
    "declared_capability_ids",
    "domain_observers",
    "publish_methods",
)
