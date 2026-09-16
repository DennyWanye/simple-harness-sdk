# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Artifact propagation and versioning across the DAG (§20.3, plan D3-7'/D3-8').

A downstream Attempt starts from the *accepted* artifacts of **all** its ancestors,
applied in topological order: a later producer overrides an earlier one only when
it depends (transitively) on that producer — it saw the earlier file and changed
it on purpose.  Two producers that are independent of each other (parallel
branches) giving the same path *different* content is an ``ArtifactConflict``
(never silently pick one); the same content is not a conflict.  The resulting
``(task_id, path, content_hash)`` set is frozen into the dispatch intent as the
Attempt's inputs, so a later inspection can tell exactly which upstream version a
result was built on — and the verifier re-materialises those inputs as protected
files (a downstream Worker may not rewrite what it was given).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import Artifact, Task
from ..contracts.evidence_state import ValidityWitness
from ..contracts.htn import OccurrenceId, TaskSemanticBindingV1
from ..contracts.models import ContractError
from ..graph.projection_validation import require_topological_order
from ..graph.task_network import TaskNetworkSnapshot
from .input_bindings import (
    AcceptedOutputsIndex,
    InputManifest,
    ResolutionPolicy,
    ResolutionProblem,
    ResolutionResult,
    TargetRules,
    materialise_plan,
    resolve_declared_inputs,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .workspace import Workspace


class ArtifactConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpstreamInput:
    task_id: str
    path: str
    content_hash: str
    artifact_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "path": self.path,
            "content_hash": self.content_hash,
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> UpstreamInput:
        return cls(
            str(value["task_id"]),
            str(value["path"]),
            str(value["content_hash"]),
            str(value["artifact_id"]),
        )


def ancestors(task_id: str, tasks_by_id: Mapping[str, Task]) -> list[Task]:
    """All transitive dependencies of ``task_id`` in topological order (step 5 D5-3': a
    BLOCKED Task rewired to a later-created dependency keeps its ordinal, so the order
    is computed from the edges; ordinal only breaks ties)."""

    seen: set[str] = set()

    def visit(current: str) -> None:
        task = tasks_by_id.get(current)
        if task is None:
            return
        for dep in task.dependency_ids:
            if dep not in seen:
                seen.add(dep)
                visit(dep)

    visit(task_id)
    # review P2-2 / D5-4: a superseded (CANCELLED) ancestor contributes nothing — its
    # accepted artifacts belong to the plan the Manager retired
    live = [tasks_by_id[t] for t in seen if str(tasks_by_id[t].status) != "CANCELLED"]
    return topological(live, tasks_by_id)


def topological(tasks: Sequence[Task], tasks_by_id: Mapping[str, Task]) -> list[Task]:
    """Kahn over the given tasks' dependency edges (restricted to the set), ordinal as
    the deterministic tie-break."""

    members = {task.id: task for task in tasks}
    indegree = {tid: sum(1 for d in t.dependency_ids if d in members) for tid, t in members.items()}
    ready = sorted((tid for tid, n in indegree.items() if n == 0), key=_ordinal)
    order: list[Task] = []
    while ready:
        current = ready.pop(0)
        order.append(members[current])
        for tid, task in members.items():
            if current in task.dependency_ids:
                indegree[tid] -= 1
                if indegree[tid] == 0:
                    ready.append(tid)
                    ready.sort(key=_ordinal)
    if len(order) != len(members):  # a cycle can only come from a corrupted library
        order.extend(
            sorted((t for t in members.values() if t not in order), key=lambda t: _ordinal(t.id))
        )
    return order


def _ordinal(task_id: str) -> int:
    try:
        return int(task_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def merge_accepted(
    producers: Sequence[Task],
    artifacts_by_task: Mapping[str, Sequence[Artifact]],
    *,
    tasks_by_id: Mapping[str, Task],
) -> list[UpstreamInput]:
    """Accepted artifacts of ``producers`` (topological order) merged by path.

    Override is legal only along a dependency chain; independent producers with
    different content for one path raise ``ArtifactConflict``.
    """

    closure = {task.id: {t.id for t in ancestors(task.id, tasks_by_id)} for task in producers}
    inputs: dict[str, UpstreamInput] = {}
    for task in topological(list(producers), tasks_by_id):
        accepted = set(task.accepted_artifacts)
        for artifact in artifacts_by_task.get(task.id, ()):
            if artifact.id not in accepted:
                continue
            existing = inputs.get(artifact.path)
            if existing is not None and existing.content_hash != artifact.content_hash:
                if existing.task_id not in closure.get(task.id, set()):
                    raise ArtifactConflict(
                        f"{artifact.path} is produced by both {existing.task_id} and {task.id}"
                        " (independent branches) with different content"
                    )
            inputs[artifact.path] = UpstreamInput(
                task.id, artifact.path, artifact.content_hash, artifact.id
            )
    return [inputs[path] for path in sorted(inputs)]


def collect_upstream_inputs(
    task: Task, tasks_by_id: Mapping[str, Task], artifacts_by_task: Mapping[str, Sequence[Artifact]]
) -> list[UpstreamInput]:
    """What a new Attempt of ``task`` starts from: every ancestor's accepted artifacts."""

    return merge_accepted(
        ancestors(task.id, tasks_by_id), artifacts_by_task, tasks_by_id=tasks_by_id
    )


def materialise_inputs(
    workspace_root: Path, inputs: Sequence[UpstreamInput], artifacts_by_id: Mapping[str, Artifact]
) -> list[str]:
    """Copy each upstream artifact file into the workspace (verifying its hash first)."""

    from .store import ArtifactStoreError, read_verified

    written = []
    for item in inputs:
        artifact = artifacts_by_id[item.artifact_id]
        try:  # P3.2 D3: the stored bytes, hash re-checked, never through a symlink
            data = read_verified(artifact) if artifact.content_hash == item.content_hash else None
        except ArtifactStoreError:
            data = None
        if data is None:
            raise ArtifactConflict(
                f"upstream artifact {item.artifact_id} ({item.path}) is missing or changed"
            )
        target = workspace_root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(item.path)
    return written


def next_versions(existing: Sequence[Artifact]) -> dict[str, int]:
    """Highest recorded version per path (Mission-wide lineage, D3-8')."""

    versions: dict[str, int] = {}
    for artifact in existing:
        versions[artifact.path] = max(versions.get(artifact.path, 0), artifact.version)
    return versions


# ======================================================================================
# P2.3b: the hierarchical mode's path (§24.1 decisions 3, 4 and 11; TG §10.1-10.3)
# ======================================================================================
#
# Everything above this line is the legacy mode and stays exactly as it was: §18.2 asks
# for ``merge_accepted`` / ``collect_upstream_inputs`` / ``topological`` byte-for-byte
# unchanged, and the suite hashes their source to hold that.  The diagnostic readers
# (``observability/traces.py``, ``observability/evaluation.py``) keep calling those
# three and therefore keep tolerating a damaged graph, which is the whole point of
# decision 11: an operator's view of a broken Mission must still render.
#
# Below the line is the new mode, and it differs in three ways that are not details:
#
# 1. **Nothing is swept.**  A consumer starts from its resolved ``InputManifest`` and
#    from nothing else.  An ORDER-only predecessor contributes no file however many
#    artifacts it accepted — ORDER is a release condition, never a read permission
#    (§24.1 decision 1).
# 2. **A path collision is refused, not ranked.**  Two different hashes aimed at one
#    target place raise :class:`ArtifactConflict`; "B is further down the topological
#    order" is not a reason to pick B (TG §10.2).  The same hash at one place is one
#    file two bindings agree on, which is not a conflict — the same rule the legacy
#    merge already applied to exact paths, so decision 4's "do not relax the old
#    protection before the new path is wired" holds.
# 3. **A damaged projection stops the work.**  On this path an unorderable projection
#    raises :class:`~..graph.projection_validation.GraphIntegrityError` rather than
#    returning the healthy prefix, because the healthy prefix is a partial order and
#    not a plan.


def _require_orderable(network: TaskNetworkSnapshot | None) -> None:
    """Refuse to execute or materialise against a projection that cannot be ordered."""

    if network is None:
        return
    require_topological_order(network.execution_projection())


def _refuse(problems: Sequence[ResolutionProblem]) -> ArtifactConflict:
    """One conflict, with the candidates named.

    TG §10.2 asks the caller to *choose, convert or synthesise* — which it cannot do
    without knowing what the candidates were, so the artifact ids travel in the
    message and not only in the structured problem.
    """

    return ArtifactConflict(
        "; ".join(
            f"{problem.kind!s}: {problem.detail}"
            + (f" (candidates: {', '.join(problem.candidates)})" if problem.candidates else "")
            for problem in problems
        )
    )


def resolve_input_manifest(
    consumer: TaskSemanticBindingV1,
    network: TaskNetworkSnapshot,
    accepted: AcceptedOutputsIndex,
    *,
    consumer_occurrence: OccurrenceId,
    witnesses: Mapping[str, ValidityWitness],
    policy: ResolutionPolicy,
    check_topology: bool = True,
) -> ResolutionResult:
    """The new mode's answer to "what does this task read?" (TG §10.1).

    The declared DATA requirements of ``consumer_occurrence`` — *only* those — are
    resolved against the accepted outputs.  The requirement rows come from the
    network, so a requirement aimed at another consumer is not silently absorbed:
    :func:`~.input_bindings.resolve_declared_inputs` refuses it as
    ``FOREIGN_REQUIREMENT``.

    ``check_topology`` is True because this is the execution path.  A caller that is
    only *explaining* a Mission passes False and gets whatever the damaged network
    can still say, which is the diagnostic tolerance decision 11 preserves.
    """

    if not isinstance(network, TaskNetworkSnapshot):
        raise ContractError("resolve_input_manifest expects a TaskNetworkSnapshot")
    if check_topology:
        _require_orderable(network)
    requirements = [
        item
        for item in network.data_requirements
        if item.consumer_occurrence == consumer_occurrence
    ]
    return resolve_declared_inputs(
        consumer,
        requirements,
        accepted,
        witnesses=witnesses,
        policy=policy,
        consumer_occurrence=consumer_occurrence,
    )


def manifest_upstream_inputs(
    manifest: InputManifest,
    target_rules: TargetRules,
    *,
    network: TaskNetworkSnapshot | None = None,
) -> list[UpstreamInput]:
    """The manifest as the dispatch-intent input set (one entry per target place).

    Returned as :class:`UpstreamInput` on purpose: the recorded shape of "what this
    Attempt was built on" does not change between the modes, only *how the set is
    decided* does.  ``task_id`` therefore names the producer occurrence's task and
    ``path`` the place in the consumer's namespace, so a later inspection reads the
    same four fields whichever mode produced them.
    """

    _require_orderable(network)
    plan = materialise_plan(manifest, target_rules)
    if plan.problems:
        raise _refuse(plan.problems)
    producer_of = {binding.binding_id: binding.producer_task_ref for binding in manifest.bindings}
    inputs: list[UpstreamInput] = []
    for entry in plan.entries:
        producer = next(
            (str(producer_of[item]) for item in entry.binding_ids if item in producer_of),
            "",
        )
        inputs.append(
            UpstreamInput(
                producer,
                entry.target.path,
                entry.content_hash,
                entry.artifact_ids[0] if entry.artifact_ids else "",
            )
        )
    return sorted(inputs, key=lambda item: item.path)


def materialise_v2(
    workspace: Workspace,
    manifest: InputManifest,
    artifacts_by_id: Mapping[str, Artifact],
    *,
    target_rules: TargetRules,
    network: TaskNetworkSnapshot | None = None,
) -> list[str]:
    """Write exactly the manifest's entries into ``workspace`` and nothing else.

    The bytes still come through :func:`~.store.read_verified` — hash re-checked,
    never through a symlink — and the placement still goes through
    :meth:`~.workspace.Workspace.materialise_manifest`, so the CAS and isolation
    guarantees of the old path are unchanged.  What changed is the *set*.

    **Not called from ``src`` yet — P2.3c wires it.**  P2.3b decides the *set* of
    inputs (``manifest_upstream_inputs`` feeds the dispatch intent, which the
    existing workspace binding then materialises through the old, unchanged path).
    Moving the physical write onto this function means changing where a verification
    copy and a protected file come from, and that belongs with the dispatch work
    rather than beside it.
    """

    _require_orderable(network)
    plan = materialise_plan(manifest, target_rules)
    if plan.problems:
        raise _refuse(plan.problems)
    from .store import ArtifactStoreError, read_verified

    blobs: dict[str, bytes] = {}
    for entry in plan.entries:
        artifact = artifacts_by_id.get(entry.artifact_ids[0] if entry.artifact_ids else "")
        if artifact is None or artifact.content_hash != entry.content_hash:
            raise ArtifactConflict(
                f"manifest entry {entry.target.path!r} names artifact "
                f"{entry.artifact_ids!r}, which is missing or changed"
            )
        try:
            blobs[entry.target.path] = read_verified(artifact)
        except ArtifactStoreError as error:
            raise ArtifactConflict(
                f"manifest entry {entry.target.path!r} is not readable: {error}"
            ) from error
    return workspace.materialise_manifest(plan.entries, blobs)


__all__ = (
    "ArtifactConflict",
    "UpstreamInput",
    "ancestors",
    "topological",
    "collect_upstream_inputs",
    "manifest_upstream_inputs",
    "materialise_inputs",
    "materialise_v2",
    "merge_accepted",
    "next_versions",
    "resolve_input_manifest",
)
