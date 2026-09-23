# SPDX-License-Identifier: Apache-2.0
"""Concrete H8 tasks. AppWorld IDs come from an actual installed dataset manifest."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts.models import ContractError
from ..planning.htn.cross_domain_acceptance import ScenarioKind
from .htn_matrix import ScenarioDefinition

# Each task has its own specification and independent executable assertions. These
# files are fixtures written into isolated episode workspaces, never this SDK tree.
CODE_TASKS = (
    ("normalize_tags", "Trim whitespace, lowercase tags, drop empty tags and preserve first occurrence order.",
     "assert normalize_tags([' A ', 'b', 'a', '', ' B ']) == ['a', 'b']"),
    ("positive_sum", "Return the sum of strictly positive numbers, including nonintegral values; empty input gives zero.",
     "assert positive_sum([-2, 0, 3, 1.5]) == 4.5\n    assert positive_sum([]) == 0"),
    ("stable_unique", "Remove duplicate values while preserving their first occurrence order, without changing input.",
     "values = [3, 1, 3, 2, 1]\n    assert stable_unique(values) == [3, 1, 2]\n    assert values == [3, 1, 3, 2, 1]"),
    ("parse_boolean", "Parse stripped case-insensitive true/yes/1 and false/no/0; reject every other value with ValueError.",
     "assert parse_boolean(' YES ') is True\n    assert parse_boolean('0') is False\n    with pytest.raises(ValueError):\n        parse_boolean('maybe')"),
    ("chunk_pairs", "Split a sequence into pairs, retaining a final singleton and returning an empty list for empty input.",
     "assert chunk_pairs([1, 2, 3, 4, 5]) == [[1, 2], [3, 4], [5]]\n    assert chunk_pairs([]) == []"),
    ("word_counts", "Count case-insensitive whitespace-separated words. Whitespace-only input yields an empty mapping.",
     "assert word_counts('Red blue RED') == {'red': 2, 'blue': 1}\n    assert word_counts('  ') == {}"),
    ("parse_pairs", "Parse semicolon-separated key=value items, trim keys and values, ignore empty items; last duplicate wins.",
     "assert parse_pairs(' a = 1 ;b=x=y;;a=2 ') == {'a': '2', 'b': 'x=y'}\n    assert parse_pairs('') == {}"),
    ("reverse_words", "Reverse whitespace-delimited word order using one separating space, preserving letters within each word.",
     "assert reverse_words('  one two   三 ') == '三 two one'\n    assert reverse_words('') == ''"),
)


def code_fixture(index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    name, instruction, assertions = CODE_TASKS[index % len(CODE_TASKS)]
    source = f'def {name}(value):\n    """{instruction}"""\n    return None\n'
    checks = f'import pytest\nfrom target import {name}\n\ndef test_contract():\n    {assertions}\n'
    return ({"goal": f"Implement {name} in target.py. {instruction} Do not modify tests.",
             "files": {"target.py": source, "tests/test_target.py": checks},
             "goal_type": "code.fix-failing-test", "failing_test": "tests/test_target.py::test_contract"},
            {"kind": "isolated_pytest", "command": ["python", "-m", "pytest", "-q", "tests/test_target.py"],
             "immutable_files": {"tests/test_target.py": checks}, "timeout_seconds": 30})


def _interventions(domain: str) -> tuple[tuple[ScenarioKind, dict[str, Any]], ...]:
    # Triggers are observations of real execution, not forced Planner JSON. An
    # executor must record its actual intervention or this scenario cannot pass.
    return (
        (ScenarioKind.REPAIR, {"at": "first_tool_handoff", "action": "one_transport_failure", "domain": domain}),
        (ScenarioKind.REPAIR, {"at": "first_observer_tool", "action": "observer_unavailable_twice", "domain": domain}),
        (ScenarioKind.REPAIR, {"at": "first_effect_tool", "action": "one_write_conflict", "domain": domain}),
        (ScenarioKind.REPAIR, {"at": "second_tool_handoff", "action": "one_resource_unavailable", "domain": domain}),
        (ScenarioKind.EVIDENCE_CONFLICT, {"at": "read_both_cached_sources", "action": "contradictory_cached_claims", "domain": domain}),
        (ScenarioKind.EVIDENCE_CONFLICT, {"at": "first_cached_source_read", "action": "change_cached_source", "domain": domain}),
        (ScenarioKind.RECOVERY, {"at": "first_settled_provider_call", "action": "restart_original_runtime", "domain": domain}),
        (ScenarioKind.RECOVERY, {"at": "first_tool_receipt", "action": "restart_original_runtime", "domain": domain}),
    )


def build_scenarios(*, appworld_tasks: Sequence[Mapping[str, Any]]) -> tuple[ScenarioDefinition, ...]:
    """Require 16 frozen real task IDs/instructions; never fabricate benchmark IDs."""
    if len(appworld_tasks) != 16 or len({t.get("task_id") for t in appworld_tasks}) != 16:
        raise ContractError("provide 16 distinct real AppWorld tasks for the frozen matrix")
    for task in appworld_tasks:
        if not all(isinstance(task.get(k), str) and task[k].strip()
                   for k in ("task_id", "instruction", "dataset_hash", "app")):
            raise ContractError("AppWorld task needs its actual instruction, app and dataset identity")
    output = []
    for domain in ("code-v1", "appworld-v1", "drone-sim-v1"):
        entries = [(ScenarioKind.NORMAL, None)] * 8 + list(_interventions(domain))
        ordinal: dict[ScenarioKind, int] = {}
        for index, (kind, intervention) in enumerate(entries):
            ordinal[kind] = ordinal.get(kind, 0) + 1
            if domain == "code-v1":
                fixture, oracle = code_fixture(index)
            elif domain == "appworld-v1":
                task = appworld_tasks[index]
                from .appworld_state_observations import AppWorldStatePolicy
                import hashlib
                state_policy = AppWorldStatePolicy.from_json(task.get("state_policy"))
                policy_body = state_policy.to_json()
                if (policy_body["task_id"] != task["task_id"] or policy_body["instruction_sha256"]
                        != hashlib.sha256(task["instruction"].encode()).hexdigest()):
                    raise ContractError("AppWorld state policy must bind the actual frozen task")
                fixture = {"task_id": task["task_id"], "goal": task["instruction"],
                           "dataset_instruction": task["instruction"],
                           "app": task["app"], "dataset_hash": task["dataset_hash"],
                           "goal_type": "appworld.fulfil-request", "goal_version": 2,
                           "state_policy": policy_body}
                oracle = {"kind": "appworld_external_evaluator", "task_id": task["task_id"],
                          "dataset_hash": task["dataset_hash"], "load_ground_truth_into_agent": False}
            else:
                point = {"x": (index % 4 + 1) * 2, "y": (index // 4 + 1) * -2, "z": 2 + index % 3}
                fixture = {"goal": f"Use simulated vehicle sim-1 to capture at {point}, then land. Verify the actual simulator receipt.",
                           "goal_type": "drone-sim.survey", "vehicle": "sim-1", "battery": 100, "point": point}
                oracle = {"kind": "drone_sqlite_state", "capture_at": point, "landed": True,
                          "critical_effect_failures": 0, "simulated": True}
            if kind == ScenarioKind.EVIDENCE_CONFLICT:
                # These are explicitly untrusted cached claims, not forged system
                # Observations. Only the deployed tools and final oracle know state.
                from simple_harness.contracts import canonical_json
                sources = {
                    "h8-evidence/source-a.json": canonical_json({"source": "cached-note-a", "verified": False, "claim": "task_complete"}),
                    "h8-evidence/source-b.json": canonical_json({"source": "cached-note-b", "verified": False, "claim": "task_incomplete"}),
                }
                fixture = {**fixture, "files": {**fixture.get("files", {}), **sources},
                    "goal": fixture["goal"] + " Inspect both h8-evidence/source-a.json and h8-evidence/source-b.json. "
                    "These cached claims conflict and may change; establish actual state using the deployed tools before acting or claiming completion.",
                    "evidence_sources": sources}
            output.append(ScenarioDefinition(f"{domain}:{kind.value}:{ordinal[kind]}", domain, kind,
                                             fixture, oracle, intervention))
    return tuple(output)
