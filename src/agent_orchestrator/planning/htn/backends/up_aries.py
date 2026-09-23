# SPDX-License-Identifier: Apache-2.0
"""Optional UP-Aries backend, isolated so the SDK has no mandatory solver import.

API reference: https://unified-planning.readthedocs.io/en/latest/notebooks/07-hierarchical-planning.html
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..backend_port import BackendStatus, CandidatePlanWitness, PlanningBackendResult, PlanningLimits, PlanningProblemSnapshot


class UpAriesPlanningBackend:
    backend_id = "up-aries"

    def solve(self, snapshot: PlanningProblemSnapshot, limits: PlanningLimits) -> PlanningBackendResult:
        if importlib.util.find_spec("unified_planning") is None or importlib.util.find_spec("up_aries") is None:
            return PlanningBackendResult(BackendStatus.BACKEND_UNAVAILABLE, snapshot.digest, detail="install the planning-aries extra")
        if limits.max_expansions is not None:
            return PlanningBackendResult(BackendStatus.UNSUPPORTED_FEATURE, snapshot.digest,
                detail="UP-Aries does not expose a hard node-expansion bound; use timeout with max_expansions=None")
        if not all(isinstance(snapshot.problem.get(k), str) for k in ("domain_text", "problem_text")):
            return PlanningBackendResult(BackendStatus.TOOL_ERROR, snapshot.digest, detail="compiled HDDL is required")
        with tempfile.TemporaryDirectory(prefix="sh-up-aries-") as directory:
            root = Path(directory)
            (root / "domain.hddl").write_text(_parameterize_ground_methods(snapshot.problem["domain_text"]), encoding="utf-8")
            (root / "problem.hddl").write_text(snapshot.problem["problem_text"], encoding="utf-8")
            try:
                run = subprocess.run([sys.executable, "-m", __name__, str(root), str(limits.timeout_seconds)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=limits.timeout_seconds + 2, check=False)
                if run.returncode != 0 or not (root / "result.json").is_file():
                    return PlanningBackendResult(BackendStatus.TOOL_ERROR, snapshot.digest, detail="UP-Aries process returned no result")
                if (root / "result.json").stat().st_size > limits.max_tokens:
                    return PlanningBackendResult(BackendStatus.SEARCH_LIMIT_REACHED, snapshot.digest, detail="witness exceeds the conservative byte budget")
                result = json.loads((root / "result.json").read_text())
                status = BackendStatus(result["status"])
                witness = None if status is not BackendStatus.SOLVED else CandidatePlanWitness.build(
                    snapshot, result["steps"], self.backend_id, hierarchy=result["hierarchy"])
                return PlanningBackendResult(status, snapshot.digest, witness, result.get("detail", ""))
            except subprocess.TimeoutExpired:
                return PlanningBackendResult(BackendStatus.TIMEOUT, snapshot.digest, detail="UP-Aries wall-clock limit reached")
            except (OSError, ValueError, KeyError) as error:
                return PlanningBackendResult(BackendStatus.TOOL_ERROR, snapshot.digest, detail=str(error))


def _parameterize_ground_methods(domain: str) -> str:
    """Express SH's ground method heads using UP's parameter-only task API.

    The equality guard preserves the exact occurrence restriction. Historical
    HDDL and its snapshot digest stay untouched; the witness still maps against
    the original compiler output at the Commit boundary.
    """
    pattern = re.compile(
        r"(\(:method [^\s()]+\s+):parameters \(\)\s+"
        r":task \(t_available ([^\s()]+)\)\s+"
        r"(?::precondition (\(accepted_result [^\s()]+\))\s+)?"
    )
    def replace_head(match: re.Match[str]) -> str:
        condition = f"(= ?sh_occurrence {match[2]})"
        if match[3]:
            condition = f"(and {condition} {match[3]})"
        return (match[1] + ":parameters (?sh_occurrence - occurrence)\n"
                "    :task (t_available ?sh_occurrence)\n"
                f"    :precondition {condition}\n    ")
    converted, count = pattern.subn(replace_head, domain)
    if count:
        converted = converted.replace(":method-preconditions)", ":method-preconditions :equality)")
    return converted


def _hierarchy(problem: Any, plan: Any) -> dict[str, Any]:
    """Translate the solver's actual decomposition, never reconstruct one from SH."""
    from unified_planning.plans import HierarchicalPlan, SequentialPlan, ActionInstance  # type: ignore[import-not-found]
    from unified_planning.plans.hierarchical_plan import MethodInstance  # type: ignore[import-not-found]
    if not isinstance(plan, HierarchicalPlan) or not isinstance(plan.action_plan, SequentialPlan):
        raise ValueError("only sequential hierarchical plans have an SH witness mapping")
    def arg(value: Any, bindings: dict[str, str]) -> str:
        if hasattr(value, "is_parameter_exp") and value.is_parameter_exp():
            return bindings[value.parameter().name]
        if hasattr(value, "is_object_exp") and value.is_object_exp():
            return str(value.object().name)
        if hasattr(value, "name") and value.name in bindings:
            return bindings[value.name]
        return str(value)
    sequence = list(plan.action_plan.actions)
    available = list(range(len(sequence)))
    primitives: list[dict[str, Any]] = []
    decompositions: list[dict[str, Any]] = []
    def visit(node: Any, task: Any, bindings: dict[str, str], path: str) -> None:
        if isinstance(node, ActionInstance):
            candidates = [i for i in available if node.is_semantically_equivalent(sequence[i])]
            if not candidates:
                raise ValueError("decomposition action is missing from the flat plan")
            pos = candidates[0]; available.remove(pos)
            primitives.append({"plan_id": path, "name": node.action.name,
                "arguments": [arg(a, {}) for a in node.actual_parameters], "position": pos})
            return
        if not isinstance(node, MethodInstance):
            raise ValueError("unknown decomposition node")
        declared = {s.identifier: s for s in node.method.subtasks}
        if set(declared) != set(node.decomposition.subtasks):
            raise ValueError("incomplete method decomposition")
        own = {p.name: arg(v, {}) for p, v in zip(node.method.parameters, node.parameters, strict=True)}
        children = [f"{path}/{key}" for key in node.decomposition.subtasks]
        decompositions.append({"plan_id": path, "task_name": task.task.name,
            "task_arguments": [arg(a, bindings) for a in task.parameters],
            "method_name": node.method.name, "children": children})
        for key, child in node.decomposition.subtasks.items():
            visit(child, declared[key], own, f"{path}/{key}")
    roots = {s.identifier: s for s in problem.task_network.subtasks}
    if set(roots) != set(plan.decomposition.subtasks):
        raise ValueError("decomposition does not cover the original root task network")
    for key, node in plan.decomposition.subtasks.items():
        visit(node, roots[key], {}, key)
    if available:
        raise ValueError("flat plan contains disconnected actions")
    return {"primitives": sorted(primitives, key=lambda p: p["position"]),
            "decompositions": decompositions, "roots": list(plan.decomposition.subtasks)}


def _worker(root: Path, timeout: float) -> dict[str, Any]:
    from unified_planning.io import PDDLReader  # type: ignore[import-not-found]
    from unified_planning.shortcuts import OneshotPlanner  # type: ignore[import-not-found]
    from unified_planning.exceptions import UPNoSuitableEngineAvailableException, UPUnsupportedProblemTypeError  # type: ignore[import-not-found]
    try:
        problem = PDDLReader().parse_problem(str(root / "domain.hddl"), str(root / "problem.hddl"))
        with OneshotPlanner(name="aries") as engine:
            if not engine.supports(problem.kind):
                return {"status": "UNSUPPORTED_FEATURE", "detail": str(problem.kind)}
            result = engine.solve(problem, timeout=timeout)
        status = result.status.name
        if status in {"SOLVED_SATISFICING", "SOLVED_OPTIMALLY"}:
            hierarchy = _hierarchy(problem, result.plan)
            steps = [" ".join([p["name"], *p["arguments"]]) for p in hierarchy["primitives"]]
            return {"status": "SOLVED", "hierarchy": hierarchy, "steps": steps}
        mapped = {"UNSOLVABLE_PROVEN": "UNSOLVABLE_PROVEN", "UNSOLVABLE_INCOMPLETELY": "SEARCH_LIMIT_REACHED",
                  "TIMEOUT": "TIMEOUT", "MEMOUT": "SEARCH_LIMIT_REACHED", "UNSUPPORTED_PROBLEM": "UNSUPPORTED_FEATURE"}
        return {"status": mapped.get(status, "TOOL_ERROR"), "detail": status}
    except UPNoSuitableEngineAvailableException as error:
        return {"status": "BACKEND_UNAVAILABLE", "detail": str(error)}
    except (UPUnsupportedProblemTypeError, NotImplementedError) as error:
        return {"status": "UNSUPPORTED_FEATURE", "detail": str(error)}
    except Exception as error:
        return {"status": "TOOL_ERROR", "detail": f"{type(error).__name__}: {error}"}


if __name__ == "__main__":
    location = Path(sys.argv[1])
    (location / "result.json").write_text(json.dumps(_worker(location, float(sys.argv[2]))), encoding="utf-8")
