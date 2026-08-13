# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""DeskPet workflow definition compiler and native execution adapter."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import logging
import textwrap
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from simple_harness.contracts import freeze_json

from .contracts import (
    ChannelSpec,
    EffectKind,
    JsonType,
    JsonValue,
    NodeExecutionIdentity,
    NodeHandler,
    PureRouteContext,
    ReducerKind,
    RetryPolicy,
    RouteSelector,
    StatePatch,
    ToolAccess,
    ToolInventoryEntry,
    WorkflowContext,
    WorkflowState,
    canonical_json,
    validate_json_value,
)
from .control import WorkflowSuspended
from .errors import (
    InvalidStatePatch,
    StateMergeConflict,
    WorkflowDefinitionError,
    WorkflowErrorCode,
    WorkflowNodeError,
)

if TYPE_CHECKING:
    from .native import (
        NativeCheckpointStore,
        TerminalCommitProjectionPort,
        TerminalProjectionDescriptor,
        TerminalProjectionPort,
        WorkflowObserverPort,
        WorkflowProgressPort,
    )

logger = logging.getLogger(__name__)


END_NODE = "__end__"


class DurabilityMode(StrEnum):
    SYNC = "sync"


class NodeDispatch(StrEnum):
    SINGLE = "single"
    PARALLEL = "parallel"


@dataclass(frozen=True)
class NodeDefinition:
    node_id: str
    handler: NodeHandler
    required: bool = True
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    interrupt_capable: bool = False
    barrier: bool = False
    exclusive_superstep: bool = False
    dispatch: NodeDispatch | str = NodeDispatch.SINGLE
    pre_interrupt_effect_policy: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id or self.node_id == END_NODE:
            raise WorkflowDefinitionError(
                "invalid_node_id", f"Invalid workflow node id: {self.node_id!r}"
            )
        if not callable(self.handler):
            raise WorkflowDefinitionError(
                "invalid_node_handler", f"Node {self.node_id} handler is not callable"
            )
        try:
            dispatch = NodeDispatch(self.dispatch)
        except ValueError as exc:
            raise WorkflowDefinitionError(
                "invalid_node_dispatch",
                f"Node {self.node_id} has unknown dispatch mode {self.dispatch!r}",
            ) from exc
        object.__setattr__(self, "dispatch", dispatch)
        if self.interrupt_capable and self.pre_interrupt_effect_policy != "pure":
            raise WorkflowDefinitionError(
                "unsafe_interrupt_policy",
                f"Interrupt-capable node {self.node_id} must declare pure pre-interrupt work",
            )
        if not self.interrupt_capable and self.pre_interrupt_effect_policy is not None:
            raise WorkflowDefinitionError(
                "unexpected_interrupt_policy",
                f"Non-interrupt node {self.node_id} may not declare pre-interrupt policy",
            )


@dataclass(frozen=True)
class Edge:
    source: str | tuple[str, ...]
    target: str

    def __post_init__(self) -> None:
        sources = (self.source,) if isinstance(self.source, str) else tuple(self.source)
        if not sources or any(not source for source in sources):
            raise WorkflowDefinitionError("invalid_edge", "Edge source is required")
        if len(set(sources)) != len(sources):
            raise WorkflowDefinitionError(
                "invalid_join_edge", "Join edge sources must be unique"
            )
        if not self.target:
            raise WorkflowDefinitionError("invalid_edge", "Edge target is required")
        object.__setattr__(self, "source", sources)

    @property
    def sources(self) -> tuple[str, ...]:
        return self.source  # type: ignore[return-value]


@dataclass(frozen=True)
class ConditionalEdge:
    source: str
    selector: RouteSelector
    routes: Mapping[str, str]
    selector_effect_policy: str | None = None

    def __post_init__(self) -> None:
        if not self.source or not callable(self.selector) or not self.routes:
            raise WorkflowDefinitionError(
                "invalid_conditional_edge",
                "Conditional edge requires a source, selector and routes",
            )
        routes = dict(self.routes)
        if any(not key or not target for key, target in routes.items()):
            raise WorkflowDefinitionError(
                "invalid_conditional_edge", "Conditional routes may not be empty"
            )
        object.__setattr__(self, "routes", MappingProxyType(routes))
        if self.selector_effect_policy != "pure":
            raise WorkflowDefinitionError(
                "unsafe_route_selector",
                f"Conditional selector {self.source} must declare pure effects",
            )


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: str
    state_schema_version: int
    entry_node: str
    nodes: Sequence[NodeDefinition]
    channels: Mapping[str, ChannelSpec]
    recursion_limit: int
    max_supersteps: int
    edges: Sequence[Edge] = ()
    conditional_edges: Sequence[ConditionalEdge] = ()
    loop_budgets: Mapping[str, int] = field(default_factory=dict)
    loop_budget_bindings: Mapping[str, str] = field(default_factory=dict)
    prompt_manifest: Mapping[str, JsonValue] = field(default_factory=dict)
    tool_manifest: Sequence[ToolInventoryEntry] = ()
    policy_manifest: Mapping[str, JsonValue] = field(default_factory=dict)
    durability: DurabilityMode | str = DurabilityMode.SYNC
    terminal_projection_descriptor: TerminalProjectionDescriptor | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "conditional_edges", tuple(self.conditional_edges))
        object.__setattr__(self, "tool_manifest", tuple(self.tool_manifest))
        object.__setattr__(self, "channels", MappingProxyType(dict(self.channels)))
        object.__setattr__(self, "loop_budgets", MappingProxyType(dict(self.loop_budgets)))
        object.__setattr__(
            self, "loop_budget_bindings", MappingProxyType(dict(self.loop_budget_bindings))
        )
        object.__setattr__(
            self, "prompt_manifest", MappingProxyType(copy.deepcopy(dict(self.prompt_manifest)))
        )
        object.__setattr__(
            self, "policy_manifest", MappingProxyType(copy.deepcopy(dict(self.policy_manifest)))
        )


@dataclass(frozen=True)
class WorkflowManifest:
    workflow_name: str
    workflow_version: str
    state_schema_version: int
    durability: str
    recursion_limit: int
    max_supersteps: int
    definition_hash: str
    state_hash: str
    prompt_hash: str
    tool_hash: str
    policy_hash: str
    callable_source_hash: str
    dependency_lock_hash: str
    implementation_bundle_hash: str
    terminal_projection_descriptor: TerminalProjectionDescriptor | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "workflow_name": self.workflow_name,
            "workflow_version": self.workflow_version,
            "state_schema_version": self.state_schema_version,
            "durability": self.durability,
            "recursion_limit": self.recursion_limit,
            "max_supersteps": self.max_supersteps,
            "definition_hash": self.definition_hash,
            "state_hash": self.state_hash,
            "prompt_hash": self.prompt_hash,
            "tool_hash": self.tool_hash,
            "policy_hash": self.policy_hash,
            "callable_source_hash": self.callable_source_hash,
            "dependency_lock_hash": self.dependency_lock_hash,
            "implementation_bundle_hash": self.implementation_bundle_hash,
            "terminal_projection_descriptor": (
                None
                if self.terminal_projection_descriptor is None
                else self.terminal_projection_descriptor.to_dict()
            ),
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_json(value: JsonValue) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _callable_record(function: object) -> dict[str, JsonValue]:
    module = getattr(function, "__module__", type(function).__module__)
    qualname = getattr(function, "__qualname__", type(function).__qualname__)
    try:
        source = textwrap.dedent(inspect.getsource(cast(Any, function))).strip()
    except (OSError, TypeError) as exc:
        raise WorkflowDefinitionError(
            "callable_source_unavailable",
            f"Cannot create a stable manifest for {module}.{qualname}",
        ) from exc
    return {"module": module, "qualname": qualname, "source": source}


def _channel_payload(channels: Mapping[str, ChannelSpec]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], {
        name: {
            "value_type": str(spec.value_type),
            "reducer": str(spec.reducer),
            "allowed_writers": sorted(spec.allowed_writers),
            "item_id_key": spec.item_id_key,
        }
        for name, spec in sorted(channels.items())
    })


def _retry_payload(policy: RetryPolicy) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], {
        "max_attempts": policy.max_attempts,
        "initial_delay_seconds": policy.initial_delay_seconds,
        "backoff_multiplier": policy.backoff_multiplier,
        "max_delay_seconds": policy.max_delay_seconds,
        "retryable_codes": sorted(policy.retryable_codes),
    })


def _tool_payload(tool: ToolInventoryEntry) -> dict[str, JsonValue]:
    effect: dict[str, JsonValue] | None = None
    if tool.effect_policy is not None:
        effect = {
            "policy_id": tool.effect_policy.policy_id,
            "version": tool.effect_policy.version,
            "kind": str(tool.effect_policy.kind),
            "max_attempts": tool.effect_policy.max_attempts,
            "reusable_across_branches": tool.effect_policy.reusable_across_branches,
        }
    return {
        "name": tool.name,
        "access": str(tool.access),
        "spec_version": tool.spec_version,
        "schema_hash": tool.schema_hash,
        "effect_policy": effect,
        "outcome_parser_id": tool.outcome_parser_id,
        "outcome_parser_version": tool.outcome_parser_version,
        "outcome_parser_hash": tool.outcome_parser_hash,
    }


def _strongly_connected_components(
    node_ids: Sequence[str], adjacency: Mapping[str, set[str]]
) -> tuple[frozenset[str], ...]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[frozenset[str]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target in sorted(adjacency.get(node_id, set())):
            if target not in indices:
                visit(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[target])
        if lowlinks[node_id] != indices[node_id]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node_id:
                break
        result.append(frozenset(component))

    for node_id in sorted(node_ids):
        if node_id not in indices:
            visit(node_id)
    return tuple(sorted(result, key=lambda item: tuple(sorted(item))))


def _cycle_bindings(
    definition: WorkflowDefinition,
    adjacency: Mapping[str, set[str]],
) -> tuple[tuple[tuple[str, str], str], ...]:
    components = _strongly_connected_components(
        tuple(node.node_id for node in definition.nodes), adjacency
    )
    cyclic = tuple(
        component
        for component in components
        if len(component) > 1
        or any(node_id in adjacency.get(node_id, set()) for node_id in component)
    )
    bindings: list[tuple[tuple[str, str], str]] = []
    for component in cyclic:
        canonical_key = ",".join(sorted(component))
        internal_edges = {
            (source, target)
            for source in component
            for target in adjacency.get(source, set())
            if target in component
        }
        selected: dict[tuple[str, str], str] = {}
        for key, budget_name in definition.loop_budget_bindings.items():
            if "->" in key:
                source, target = key.split("->", 1)
                edge = (source.strip(), target.strip())
                if edge in internal_edges:
                    selected[edge] = budget_name
            elif key in component:
                for edge in internal_edges:
                    if edge[1] == key:
                        selected[edge] = budget_name
            elif key == canonical_key:
                for edge in internal_edges:
                    selected[edge] = budget_name
        # Compatibility for the pre-native one-loop definitions. Multiple
        # cycles are intentionally never paired with budgets by position.
        if not selected and len(cyclic) == 1 and len(definition.loop_budgets) == 1:
            budget_name = next(iter(definition.loop_budgets))
            order = {node_id: index for index, node_id in enumerate(sorted(component))}
            selected = {
                edge: budget_name
                for edge in internal_edges
                if order[edge[1]] <= order[edge[0]]
            }
        if not selected:
            raise WorkflowDefinitionError(
                "unbudgeted_cycle",
                f"Cyclic SCC {canonical_key} requires explicit feedback-edge budget bindings",
            )
        for budget_name in selected.values():
            if budget_name not in definition.loop_budgets:
                raise WorkflowDefinitionError(
                    "unknown_loop_budget_binding",
                    f"Cyclic SCC {canonical_key} references unknown budget {budget_name}",
                )
        remaining = {
            source: {
                target
                for target in adjacency.get(source, set())
                if target in component and (source, target) not in selected
            }
            for source in component
        }
        if any(
            len(part) > 1 or any(node in remaining[node] for node in part)
            for part in _strongly_connected_components(tuple(component), remaining)
        ):
            raise WorkflowDefinitionError(
                "unbudgeted_cycle",
                f"Cyclic SCC {canonical_key} contains a cycle without a bound feedback edge",
            )
        bindings.extend(sorted(selected.items()))
    return tuple(bindings)


def _validate_definition(definition: WorkflowDefinition) -> dict[str, NodeDefinition]:
    if not definition.name or not definition.version:
        raise WorkflowDefinitionError(
            "missing_workflow_identity", "Workflow name and version are required"
        )
    if definition.state_schema_version < 1:
        raise WorkflowDefinitionError(
            "invalid_state_schema", "state_schema_version must be positive"
        )
    try:
        durability = DurabilityMode(definition.durability)
    except ValueError as exc:
        raise WorkflowDefinitionError(
            "unsafe_durability", "Durable workflows require sync durability"
        ) from exc
    if durability is not DurabilityMode.SYNC:
        raise WorkflowDefinitionError(
            "unsafe_durability", "Durable workflows require sync durability"
        )
    if definition.recursion_limit < 1 or definition.max_supersteps < 1:
        raise WorkflowDefinitionError(
            "invalid_recursion_limit", "recursion limits must be positive"
        )
    if definition.recursion_limit <= definition.max_supersteps:
        raise WorkflowDefinitionError(
            "unsafe_recursion_limit",
            "recursion_limit must exceed the hard maximum supersteps",
        )
    if any(not name or not isinstance(value, int) or isinstance(value, bool) or value < 1
           for name, value in definition.loop_budgets.items()):
        raise WorkflowDefinitionError(
            "invalid_loop_budget", "Loop budgets must be named positive integers"
        )
    minimum_supersteps = len(definition.nodes) + sum(definition.loop_budgets.values())
    if definition.max_supersteps < minimum_supersteps:
        raise WorkflowDefinitionError(
            "unsafe_superstep_budget",
            "max_supersteps must cover all nodes and persisted loop budgets",
            details={"minimum_supersteps": minimum_supersteps},
        )

    node_ids = [node.node_id for node in definition.nodes]
    duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
    if duplicates:
        raise WorkflowDefinitionError(
            "duplicate_node", f"Duplicate nodes: {', '.join(duplicates)}"
        )
    nodes = {node.node_id: node for node in definition.nodes}
    if not definition.entry_node:
        raise WorkflowDefinitionError("missing_entry", "Workflow entry node is required")
    if definition.entry_node not in nodes:
        raise WorkflowDefinitionError(
            "missing_entry", f"Entry node {definition.entry_node!r} does not exist"
        )

    for name, spec in definition.channels.items():
        if not name:
            raise WorkflowDefinitionError("invalid_channel", "Channel name is required")
        try:
            value_type = JsonType(spec.value_type)
        except ValueError as exc:
            raise WorkflowDefinitionError(
                "unknown_value_type", f"Channel {name} has unknown JSON type"
            ) from exc
        try:
            reducer = ReducerKind(spec.reducer)
        except ValueError as exc:
            raise WorkflowDefinitionError(
                "unknown_reducer", f"Channel {name} has unknown reducer {spec.reducer!r}"
            ) from exc
        unknown_writers = sorted(spec.allowed_writers - nodes.keys())
        if unknown_writers:
            raise WorkflowDefinitionError(
                "unknown_channel_writer",
                f"Channel {name} has unknown writers: {', '.join(unknown_writers)}",
            )
        if reducer is ReducerKind.DICT_DISJOINT and value_type is not JsonType.OBJECT:
            raise WorkflowDefinitionError(
                "reducer_type_mismatch", f"Channel {name} dict reducer requires object"
            )
        if reducer is ReducerKind.STABLE_LIST and value_type is not JsonType.ARRAY:
            raise WorkflowDefinitionError(
                "reducer_type_mismatch", f"Channel {name} list reducer requires array"
            )

    normal_sources: set[str] = set()
    edge_keys: set[tuple[tuple[str, ...], str]] = set()
    incoming: dict[str, list[Edge]] = {node_id: [] for node_id in nodes}
    outgoing_targets: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in definition.edges:
        key = (edge.sources, edge.target)
        if key in edge_keys:
            raise WorkflowDefinitionError("duplicate_edge", f"Duplicate edge: {key!r}")
        edge_keys.add(key)
        unknown_sources = sorted(set(edge.sources) - nodes.keys())
        if unknown_sources or (edge.target != END_NODE and edge.target not in nodes):
            raise WorkflowDefinitionError(
                "invalid_edge", f"Edge references missing node: {edge!r}"
            )
        for source in edge.sources:
            normal_sources.add(source)
            outgoing_targets[source].add(edge.target)
        if edge.target != END_NODE:
            incoming[edge.target].append(edge)

    conditional_by_source: dict[str, ConditionalEdge] = {}
    for edge in definition.conditional_edges:
        if edge.source not in nodes:
            raise WorkflowDefinitionError(
                "invalid_edge", f"Conditional source {edge.source!r} does not exist"
            )
        if edge.source in conditional_by_source:
            raise WorkflowDefinitionError(
                "duplicate_conditional_edge",
                f"Node {edge.source} has multiple conditional edges",
            )
        if edge.source in normal_sources:
            raise WorkflowDefinitionError(
                "mixed_edge_modes",
                f"Node {edge.source} mixes normal and conditional edges",
            )
        missing_targets = sorted(
            {target for target in edge.routes.values() if target != END_NODE and target not in nodes}
        )
        if missing_targets:
            raise WorkflowDefinitionError(
                "invalid_edge",
                f"Conditional edge references missing nodes: {', '.join(missing_targets)}",
            )
        conditional_by_source[edge.source] = edge
        for target in edge.routes.values():
            if target != END_NODE:
                incoming[target].append(Edge(edge.source, target))

    adjacency = {node_id: set(targets) for node_id, targets in outgoing_targets.items()}
    for edge in definition.conditional_edges:
        adjacency[edge.source].update(
            target for target in edge.routes.values() if target != END_NODE
        )
    _cycle_bindings(definition, adjacency)

    reachable = {definition.entry_node}
    changed = True
    while changed:
        changed = False
        for edge in definition.edges:
            if (
                all(source in reachable for source in edge.sources)
                and edge.target != END_NODE
                and edge.target not in reachable
            ):
                reachable.add(edge.target)
                changed = True
        for edge in definition.conditional_edges:
            if edge.source in reachable:
                for target in edge.routes.values():
                    if target != END_NODE and target not in reachable:
                        reachable.add(target)
                        changed = True
    unreachable = sorted(
        node.node_id for node in definition.nodes if node.required and node.node_id not in reachable
    )
    if unreachable:
        raise WorkflowDefinitionError(
            "unreachable_required_node",
            f"Required nodes are unreachable: {', '.join(unreachable)}",
        )

    for node in definition.nodes:
        if not node.interrupt_capable:
            continue
        if not node.barrier or not node.exclusive_superstep:
            raise WorkflowDefinitionError(
                "interrupt_not_exclusive",
                f"Interrupt node {node.node_id} must be an exclusive barrier",
            )
        if node.dispatch is NodeDispatch.PARALLEL:
            raise WorkflowDefinitionError(
                "interrupt_parallel_dispatch",
                f"Interrupt node {node.node_id} may not dispatch parallel work",
            )
        inbound = incoming[node.node_id]
        if len(inbound) > 1:
            raise WorkflowDefinitionError(
                "interrupt_barrier_violation",
                f"Interrupt node {node.node_id} needs one dedicated join edge",
            )
        if inbound:
            for source in inbound[0].sources:
                if len(outgoing_targets[source]) > 1:
                    raise WorkflowDefinitionError(
                        "interrupt_barrier_violation",
                        f"Source {source} schedules interrupt {node.node_id} with siblings",
                    )
                if nodes[source].dispatch is NodeDispatch.PARALLEL:
                    raise WorkflowDefinitionError(
                        "interrupt_barrier_violation",
                        f"Parallel node {source} cannot directly schedule an interrupt",
                    )

    tool_names = [tool.name for tool in definition.tool_manifest]
    duplicate_tools = sorted({name for name in tool_names if tool_names.count(name) > 1})
    if duplicate_tools:
        raise WorkflowDefinitionError(
            "duplicate_tool_inventory",
            f"Duplicate tool inventory entries: {', '.join(duplicate_tools)}",
        )
    for tool in definition.tool_manifest:
        if tool.access is not ToolAccess.WRITE:
            continue
        if tool.effect_policy is None:
            raise WorkflowDefinitionError(
                "unclassified_write_tool", f"Write tool {tool.name} has no effect policy"
            )
        if tool.effect_policy.kind not in {EffectKind.STAGED_FILE, EffectKind.OPAQUE_MANUAL}:
            raise WorkflowDefinitionError(
                "unsafe_write_tool_policy",
                f"Write tool {tool.name} must be staged_file or opaque_manual",
            )
        if not all(
            (
                tool.outcome_parser_id,
                tool.outcome_parser_version,
                tool.outcome_parser_hash,
            )
        ):
            raise WorkflowDefinitionError(
                "missing_write_outcome_parser",
                f"Write tool {tool.name} needs a versioned outcome parser",
            )

    prompt_payload = dict(definition.prompt_manifest)
    policy_payload = dict(definition.policy_manifest)
    validate_json_value(prompt_payload)
    validate_json_value(policy_payload)
    return nodes


class CompiledWorkflow:
    """Validated domain graph consumed directly by DeskPet's native kernel."""

    def __init__(
        self,
        definition: WorkflowDefinition,
        manifest: WorkflowManifest,
        nodes: Mapping[str, NodeDefinition],
    ) -> None:
        self.definition = definition
        self.manifest = manifest
        self._nodes = MappingProxyType(dict(nodes))
        self._conditional = MappingProxyType(
            {edge.source: edge for edge in definition.conditional_edges}
        )
        self._single_targets = MappingProxyType(
            {
                node_id: tuple(
                    edge.target
                    for edge in definition.edges
                    if len(edge.sources) == 1 and edge.sources[0] == node_id
                )
                for node_id in nodes
            }
        )
        self._join_edges = tuple(edge for edge in definition.edges if len(edge.sources) > 1)
        adjacency = {node_id: set() for node_id in nodes}
        for edge in definition.edges:
            for source in edge.sources:
                if edge.target != END_NODE:
                    adjacency[source].add(edge.target)
        for edge in definition.conditional_edges:
            adjacency[edge.source].update(
                target for target in edge.routes.values() if target != END_NODE
            )
        cycle_bindings = _cycle_bindings(definition, adjacency)
        self._cycle_binding_by_edge = MappingProxyType(dict(cycle_bindings))

    def node(self, node_id: str) -> NodeDefinition:
        return self._nodes[node_id]

    def conditional_for(self, node_id: str) -> ConditionalEdge | None:
        return self._conditional.get(node_id)

    def single_targets(self, node_id: str) -> tuple[str, ...]:
        return self._single_targets.get(node_id, ())

    def join_edges_for(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self._join_edges if node_id in edge.sources)

    def is_cycle_edge(self, source: str, target: str) -> bool:
        return (source, target) in self._cycle_binding_by_edge

    def validate_loop_budget(
        self, source: str, target: str, state: Mapping[str, object]
    ) -> None:
        if not self.is_cycle_edge(source, target):
            return
        budget_name = self._cycle_binding_by_edge[(source, target)]
        counters = state.get("loop_counters", {})
        budgets = state.get("budgets", {})
        count = counters.get(budget_name, 0) if isinstance(counters, Mapping) else 0
        limit = (
            budgets.get(budget_name, self.definition.loop_budgets[budget_name])
            if isinstance(budgets, Mapping)
            else self.definition.loop_budgets[budget_name]
        )
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or count > limit
        ):
            raise InvalidStatePatch(
                "loop_budget_exhausted",
                f"Loop budget {budget_name} was exceeded",
            )

    def validate_patch(self, node_id: str, patch: StatePatch) -> None:
        if node_id not in self._nodes:
            raise InvalidStatePatch("unknown_writer", f"Unknown patch writer: {node_id}")
        values = patch.to_dict()
        unknown_channels = sorted(set(values) - self.definition.channels.keys())
        if unknown_channels:
            raise InvalidStatePatch(
                "unknown_channel",
                f"Patch writes unknown channels: {', '.join(unknown_channels)}",
            )
        for channel_name, value in values.items():
            spec = self.definition.channels[channel_name]
            if node_id not in spec.allowed_writers:
                raise InvalidStatePatch(
                    "unauthorized_channel_writer",
                    f"Node {node_id} may not write channel {channel_name}",
                )
            _validate_channel_value(channel_name, JsonType(spec.value_type), value)

    def merge_patches(self, writes: Sequence[tuple[str, StatePatch]]) -> StatePatch:
        by_channel: dict[str, list[tuple[str, JsonValue]]] = {}
        for writer, patch in writes:
            self.validate_patch(writer, patch)
            for channel, value in patch.to_dict().items():
                by_channel.setdefault(channel, []).append((writer, value))

        merged: dict[str, JsonValue] = {}
        for channel, channel_writes in by_channel.items():
            spec = self.definition.channels[channel]
            reducer = ReducerKind(spec.reducer)
            if reducer is ReducerKind.SINGLE_WRITER:
                if len(channel_writes) != 1:
                    raise StateMergeConflict(
                        "single_writer_conflict",
                        f"Parallel writes conflict on channel {channel}",
                        details={"writers": [writer for writer, _ in channel_writes]},
                    )
                merged[channel] = channel_writes[0][1]
            elif reducer is ReducerKind.DICT_DISJOINT:
                result: dict[str, JsonValue] = {}
                owners: dict[str, str] = {}
                for writer, value in channel_writes:
                    assert isinstance(value, dict)
                    overlap = sorted(result.keys() & value.keys())
                    if overlap:
                        raise StateMergeConflict(
                            "dict_key_conflict",
                            f"Parallel writes conflict on {channel}: {', '.join(overlap)}",
                            details={
                                "writer": writer,
                                "existing_writers": {key: owners[key] for key in overlap},
                            },
                        )
                    result.update(value)
                    owners.update({key: writer for key in value})
                merged[channel] = result
            else:
                items: dict[str, JsonValue] = {}
                for writer, value in channel_writes:
                    assert isinstance(value, list)
                    for item in value:
                        if not isinstance(item, dict) or spec.item_id_key not in item:
                            raise StateMergeConflict(
                                "missing_stable_item_id",
                                f"Channel {channel} item from {writer} lacks {spec.item_id_key}",
                            )
                        item_id = item[spec.item_id_key]
                        if isinstance(item_id, bool) or not isinstance(item_id, (str, int)):
                            raise StateMergeConflict(
                                "invalid_stable_item_id",
                                f"Channel {channel} item id must be string or integer",
                            )
                        stable_id = canonical_json(item_id)
                        if stable_id in items and canonical_json(items[stable_id]) != canonical_json(item):
                            raise StateMergeConflict(
                                "stable_item_conflict",
                                f"Channel {channel} has conflicting item id {item_id!r}",
                            )
                        items[stable_id] = item
                merged[channel] = [items[key] for key in sorted(items)]
        return StatePatch(merged)

    def reduce_state(self, previous: WorkflowState, delta: StatePatch) -> WorkflowState:
        state = copy.deepcopy(dict(previous))
        for channel, value in delta.to_dict().items():
            spec = self.definition.channels[channel]
            reducer = ReducerKind(spec.reducer)
            if reducer is ReducerKind.SINGLE_WRITER or channel not in state:
                state[channel] = value
                continue
            old = state[channel]
            if reducer is ReducerKind.DICT_DISJOINT:
                if not isinstance(old, dict) or not isinstance(value, dict):
                    raise StateMergeConflict(
                        "reducer_state_type_mismatch", f"Channel {channel} requires objects"
                    )
                merged = copy.deepcopy(old)
                for key, item in value.items():
                    if key in merged and canonical_json(merged[key]) != canonical_json(item):
                        raise StateMergeConflict(
                            "dict_key_conflict",
                            f"State and frontier writes conflict on {channel}.{key}",
                        )
                    merged[key] = item
                state[channel] = merged
                continue
            if not isinstance(old, list) or not isinstance(value, list):
                raise StateMergeConflict(
                    "reducer_state_type_mismatch", f"Channel {channel} requires arrays"
                )
            result = copy.deepcopy(old)
            positions: dict[str, int] = {}
            for index, item in enumerate(result):
                if not isinstance(item, dict) or spec.item_id_key not in item:
                    raise StateMergeConflict(
                        "missing_stable_item_id", f"Channel {channel} has an invalid old item"
                    )
                positions[canonical_json(item[spec.item_id_key])] = index
            for item in value:
                if not isinstance(item, dict) or spec.item_id_key not in item:
                    raise StateMergeConflict(
                        "missing_stable_item_id", f"Channel {channel} has an invalid new item"
                    )
                item_id = canonical_json(item[spec.item_id_key])
                if item_id in positions:
                    if canonical_json(result[positions[item_id]]) != canonical_json(item):
                        raise StateMergeConflict(
                            "stable_item_conflict", f"Channel {channel} has a conflicting item"
                        )
                    continue
                positions[item_id] = len(result)
                result.append(item)
            state[channel] = result
        validate_json_value(state)
        return state  # type: ignore[return-value]

    async def run_node(
        self,
        node_id: str,
        state: WorkflowState,
        context: WorkflowContext,
        execution_info: object,
    ) -> StatePatch:
        node = self._nodes[node_id]
        try:
            identity = NodeExecutionIdentity.from_execution_info(
                workflow_name=self.definition.name,
                workflow_version=self.definition.version,
                node_id=node_id,
                execution_info=execution_info,
                state=cast(Mapping[str, JsonValue], state),
            )
            result = await node.handler(
                state,
                context.for_node(
                    identity, pure_before_interrupt=node.interrupt_capable
                ),
            )
            if not isinstance(result, StatePatch):
                raise InvalidStatePatch(
                    "invalid_handler_result",
                    f"Node {node_id} must return StatePatch",
                )
            self.validate_patch(node_id, result)
            return result
        except asyncio.CancelledError:
            raise
        except WorkflowNodeError:
            raise
        except Exception as exc:
            logger.exception(
                "workflow_node_failed workflow=%s@%s node_id=%s error_type=%s",
                self.definition.name,
                self.definition.version,
                node_id,
                type(exc).__name__,
            )
            code = (
                WorkflowErrorCode.INVALID_STATE
                if isinstance(exc, (InvalidStatePatch, StateMergeConflict))
                else WorkflowErrorCode.PERMANENT
            )
            raise WorkflowNodeError(
                code=code,
                message_ref=f"workflow_node:{node_id}:{code.value}",
                node_id=node_id,
            ) from None

    async def route(
        self,
        edge: ConditionalEdge,
        state: WorkflowState,
        context: WorkflowContext,
        execution_info: object,
    ) -> str:
        try:
            identity = NodeExecutionIdentity.from_execution_info(
                workflow_name=self.definition.name,
                workflow_version=self.definition.version,
                node_id=edge.source,
                execution_info=execution_info,
                state=cast(Mapping[str, JsonValue], state),
            )
            logical_timestamp = state.get("logical_timestamp", 0.0)
            if (
                isinstance(logical_timestamp, bool)
                or not isinstance(logical_timestamp, (int, float))
            ):
                raise InvalidStatePatch(
                    "invalid_logical_timestamp",
                    "Route logical timestamp must be a frozen number",
                )
            route_context = PureRouteContext(
                workflow_name=self.definition.name,
                workflow_version=self.definition.version,
                run_id=identity.run_id,
                checkpoint_id=identity.checkpoint_id,
                task_id=identity.task_id,
                source=edge.source,
                state=cast(Mapping[str, JsonValue], state),
                logical_timestamp=float(logical_timestamp),
            )
            frozen_state = freeze_json(
                cast(JsonValue, copy.deepcopy(dict(state)))
            )
            assert isinstance(frozen_state, Mapping)
            route = edge.selector(cast(WorkflowState, frozen_state), route_context)
            if inspect.isawaitable(route):
                route = await route
            if route not in edge.routes:
                raise InvalidStatePatch(
                    "unknown_route", f"Selector {edge.source} returned an unknown route"
                )
            return route
        except asyncio.CancelledError:
            raise
        except WorkflowNodeError:
            raise
        except Exception as exc:  # noqa: BLE001 -- stable route error boundary
            code = (
                WorkflowErrorCode.INVALID_STATE
                if isinstance(exc, (InvalidStatePatch, StateMergeConflict, TypeError))
                else WorkflowErrorCode.PERMANENT
            )
            raise WorkflowNodeError(
                code=code,
                message_ref=f"workflow_route:{edge.source}:{code.value}",
                node_id=edge.source,
            ) from None

    def bind(
        self,
        *,
        store: NativeCheckpointStore,
        terminal_projection_port: TerminalProjectionPort,
        terminal_commit_projection_port: TerminalCommitProjectionPort,
        progress_port: WorkflowProgressPort | None = None,
        observer_port: WorkflowObserverPort | None = None,
    ) -> WorkflowExecutable:
        from .native import NativeWorkflowExecutable

        graph = NativeWorkflowExecutable(
            self,
            store,
            terminal_projection_port=terminal_projection_port,
            terminal_commit_projection_port=terminal_commit_projection_port,
            progress_port=progress_port,
            observer_port=observer_port,
        )
        return WorkflowExecutable(graph=graph, manifest=self.manifest)


class WorkflowExecutable:
    """Small execution facade that pins DeskPet durability and recursion."""

    def __init__(self, *, graph: object, manifest: WorkflowManifest) -> None:
        self.__graph = graph
        self.manifest = manifest

    @property
    def _is_native(self) -> bool:
        return type(self.__graph).__module__ == "simple_harness.workflow.native"

    def _config(
        self,
        *,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str,
        configurable: Mapping[str, JsonValue] | None = None,
    ) -> dict[str, object]:
        raw_configurable = dict(configurable or {})
        validate_json_value(raw_configurable, path="$.configurable")
        forwarded = copy.deepcopy(raw_configurable)
        for reserved_key, expected in (
            ("thread_id", thread_id),
            ("checkpoint_ns", checkpoint_ns),
            ("deskpet_run_id", run_id),
        ):
            supplied = forwarded.get(reserved_key)
            if supplied is not None and supplied != expected:
                raise InvalidStatePatch(
                    "conflicting_runtime_identity",
                    f"Configurable {reserved_key} conflicts with the execution identity",
                )
            forwarded[reserved_key] = expected
        return {
            "configurable": forwarded,
            "run_id": run_id,
            "recursion_limit": self.manifest.recursion_limit,
        }

    async def ainvoke(
        self,
        state: WorkflowState | object,
        context: WorkflowContext,
        *,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str = "",
        configurable: Mapping[str, JsonValue] | None = None,
    ) -> object:
        method = cast(Any, self.__graph).ainvoke
        if self._is_native:
            try:
                return await method(
                    state,
                    context,
                    thread_id=thread_id,
                    run_id=run_id,
                    checkpoint_ns=checkpoint_ns,
                    configurable=configurable,
                )
            except WorkflowSuspended as exc:
                return {"interrupt": exc.interrupt.to_dict()}
        return await method(
            state,
            config=self._config(
                thread_id=thread_id,
                run_id=run_id,
                checkpoint_ns=checkpoint_ns,
                configurable=configurable,
            ),
            context=context,
            durability=DurabilityMode.SYNC.value,
        )

    async def astream(
        self,
        state: WorkflowState | object,
        context: WorkflowContext,
        *,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str = "",
        configurable: Mapping[str, JsonValue] | None = None,
    ) -> AsyncIterator[object]:
        method = cast(Any, self.__graph).astream
        if self._is_native:
            async for item in method(
                state,
                context,
                thread_id=thread_id,
                run_id=run_id,
                checkpoint_ns=checkpoint_ns,
                configurable=configurable,
            ):
                yield item
            return
        async for item in method(
            state,
            config=self._config(
                thread_id=thread_id,
                run_id=run_id,
                checkpoint_ns=checkpoint_ns,
                configurable=configurable,
            ),
            context=context,
            durability=DurabilityMode.SYNC.value,
        ):
            yield item

    async def resume(
        self,
        responses: Mapping[str, JsonValue],
        context: WorkflowContext,
        *,
        thread_id: str,
        run_id: str,
        checkpoint_ns: str = "",
        configurable: Mapping[str, JsonValue] | None = None,
    ) -> object:
        raw_responses = dict(responses)
        validate_json_value(raw_responses, path="$.resume")
        response_payload = copy.deepcopy(raw_responses)
        if not response_payload:
            raise InvalidStatePatch(
                "empty_resume", "A workflow resume requires at least one interrupt response"
            )
        if self._is_native:
            method = cast(Any, self.__graph).resume
            try:
                return await method(
                    response_payload,
                    context,
                    thread_id=thread_id,
                    run_id=run_id,
                    checkpoint_ns=checkpoint_ns,
                    configurable=configurable,
                )
            except WorkflowSuspended as exc:
                return {"interrupt": exc.interrupt.to_dict()}
        method = cast(Any, self.__graph).ainvoke
        return await method(
            response_payload,
            config=self._config(
                thread_id=thread_id,
                run_id=run_id,
                checkpoint_ns=checkpoint_ns,
                configurable=configurable,
            ),
            context=context,
            durability=DurabilityMode.SYNC.value,
        )


def _validate_channel_value(channel: str, value_type: JsonType, value: JsonValue) -> None:
    validate_json_value(value, path=f"$.{channel}")
    valid = {
        JsonType.JSON: True,
        JsonType.STRING: isinstance(value, str),
        JsonType.INTEGER: isinstance(value, int) and not isinstance(value, bool),
        JsonType.NUMBER: isinstance(value, (int, float)) and not isinstance(value, bool),
        JsonType.BOOLEAN: isinstance(value, bool),
        JsonType.OBJECT: isinstance(value, dict),
        JsonType.ARRAY: isinstance(value, list),
    }[value_type]
    if not valid:
        raise InvalidStatePatch(
            "channel_type_mismatch",
            f"Channel {channel} requires {value_type.value}",
        )


def compile_workflow(
    definition: WorkflowDefinition,
    *,
    dependency_lock_path: str | Path | None = None,
) -> CompiledWorkflow:
    nodes = _validate_definition(definition)
    callable_records = [
        {"kind": "node", "node_id": node.node_id, **_callable_record(node.handler)}
        for node in definition.nodes
    ]
    callable_records.extend(
        {"kind": "route", "source": edge.source, **_callable_record(edge.selector)}
        for edge in definition.conditional_edges
    )
    callable_hash = _hash_json(cast(JsonValue, callable_records))

    definition_payload: dict[str, JsonValue] = {
        "name": definition.name,
        "version": definition.version,
        "entry_node": definition.entry_node,
        "nodes": [
            {
                "node_id": node.node_id,
                "required": node.required,
                "interrupt_capable": node.interrupt_capable,
                "barrier": node.barrier,
                "exclusive_superstep": node.exclusive_superstep,
                "dispatch": str(node.dispatch),
                "pre_interrupt_effect_policy": node.pre_interrupt_effect_policy,
            }
            for node in definition.nodes
        ],
        "edges": [
            {"sources": list(edge.sources), "target": edge.target}
            for edge in definition.edges
        ],
        "conditional_edges": [
            {
                "source": edge.source,
                "routes": dict(sorted(edge.routes.items())),
                "selector_effect_policy": edge.selector_effect_policy,
            }
            for edge in definition.conditional_edges
        ],
        "terminal_projection_descriptor": (
            None
            if definition.terminal_projection_descriptor is None
            else definition.terminal_projection_descriptor.to_dict()
        ),
    }
    state_payload: dict[str, JsonValue] = {
        "schema_version": definition.state_schema_version,
        "channels": _channel_payload(definition.channels),
    }
    tool_payload = [_tool_payload(tool) for tool in sorted(definition.tool_manifest, key=lambda x: x.name)]
    policy_payload: dict[str, JsonValue] = {
        "custom": dict(definition.policy_manifest),
        "durability": DurabilityMode.SYNC.value,
        "recursion_limit": definition.recursion_limit,
        "max_supersteps": definition.max_supersteps,
        "loop_budgets": dict(sorted(definition.loop_budgets.items())),
        "loop_budget_bindings": dict(sorted(definition.loop_budget_bindings.items())),
        "node_retry": {
            node.node_id: _retry_payload(node.retry_policy)
            for node in sorted(definition.nodes, key=lambda item: item.node_id)
        },
        "terminal_projection_descriptor": (
            None
            if definition.terminal_projection_descriptor is None
            else definition.terminal_projection_descriptor.to_dict()
        ),
    }

    lock_path = (
        Path(dependency_lock_path)
        if dependency_lock_path is not None
        else Path(__file__).resolve().parents[3] / "uv.lock"
    )
    if not lock_path.is_file():
        raise WorkflowDefinitionError(
            "dependency_lock_missing", f"Dependency lock does not exist: {lock_path}"
        )
    lock_bytes = lock_path.read_bytes()
    lock_hash = _sha256_bytes(lock_bytes)
    # DeepResearch v1-v4 are immutable recovery formats.  Their manifests were
    # shipped with this dependency identity and must not drift merely because
    # a later workflow version adds an unrelated product dependency (for
    # example, the v5 Playwright renderer).  Callable/definition/state/policy
    # hashes still fail closed if any historical implementation changes.
    historical_dependency_lock_hashes = {
        "lf": "1013a7b449853880a44dd4219d1fe8090dff38055fd4a42d6902887693e4c8ef",
        "crlf": "ff691f62e113477ba230f0897488fb6ec9f1d1009b003947497cbecc6ea895e5",
    }
    if definition.name == "deep_research" and definition.version in {"v1", "v2", "v3", "v4"}:
        # The registered recovery definitions keep the released LF identity on
        # every checkout. Explicit fixture compilation still exercises both
        # historical line-ending variants.
        line_ending = (
            "crlf" if dependency_lock_path is not None and b"\r\n" in lock_bytes
            else "lf"
        )
        lock_hash = historical_dependency_lock_hashes[line_ending]
    elif definition.name == "deep_research" and definition.version in {"v5", "v6"}:
        # v5/v6 were released against this dependency identity. Recovery must
        # not change when the checkout's uv.lock later advances.
        lock_hash = "0082b2a5c7d7148fa54fb5d740520430e14bfebd56f2461c5bb7adda67ec4526"
    hashes = {
        "definition_hash": _hash_json(definition_payload),
        "state_hash": _hash_json(state_payload),
        "prompt_hash": _hash_json(dict(definition.prompt_manifest)),
        "tool_hash": _hash_json(cast(JsonValue, tool_payload)),
        "policy_hash": _hash_json(policy_payload),
        "callable_source_hash": callable_hash,
        "dependency_lock_hash": lock_hash,
    }
    bundle_hash = _hash_json(
        {
            "workflow_name": definition.name,
            "workflow_version": definition.version,
            **hashes,
        }
    )
    manifest = WorkflowManifest(
        workflow_name=definition.name,
        workflow_version=definition.version,
        state_schema_version=definition.state_schema_version,
        durability=DurabilityMode.SYNC.value,
        recursion_limit=definition.recursion_limit,
        max_supersteps=definition.max_supersteps,
        implementation_bundle_hash=bundle_hash,
        terminal_projection_descriptor=definition.terminal_projection_descriptor,
        **hashes,
    )
    return CompiledWorkflow(definition, manifest, nodes)


__all__ = [
    "END_NODE",
    "CompiledWorkflow",
    "ConditionalEdge",
    "DurabilityMode",
    "Edge",
    "NodeDefinition",
    "NodeDispatch",
    "WorkflowDefinition",
    "WorkflowExecutable",
    "WorkflowManifest",
    "compile_workflow",
]
