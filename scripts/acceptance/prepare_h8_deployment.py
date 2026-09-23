#!/usr/bin/env python3
"""Prepare the explicit H8 configuration; no model, solver or episode execution."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from agent_orchestrator.evaluation.appworld_operation_profiles import MILESTONE, state_policy_refs
from agent_orchestrator.evaluation.appworld_state_observations import AppWorldStatePolicy
from agent_orchestrator.evaluation.htn_batch import _require_ignored_root
from agent_orchestrator.evaluation.htn_oracles import write_evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--appworld-data-root", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--appworld-url", default="http://127.0.0.1:18262")
    args = parser.parse_args()
    root = _require_ignored_root(args.root, ROOT)
    tasks = json.loads(args.tasks.read_text())
    if len(tasks) != 16 or len({t["task_id"] for t in tasks}) != 16:
        raise ValueError("16 distinct frozen AppWorld tasks required")
    contracts = {}
    for task in tasks:
        policy = AppWorldStatePolicy.from_json(task["state_policy"])
        evidence, milestone = state_policy_refs(policy)
        contracts[task["task_id"]] = {"mode": "REQUIRED_EFFECTS", "content_criterion_ids": [],
            "effects": [{"effect_key": "application-request", "source_slot_key": "appworld-program",
                "obligation_id": "$ROOT_OBLIGATION", "criterion_ids": ["c-actions-confirmed", "c-request-satisfied"],
                "required_milestone": MILESTONE, "milestone_policy_ref": milestone.to_json(),
                "evidence_policy_ref": evidence.to_json()}]}
    config = {"experiment_id": args.experiment_id, "checkout": str(ROOT),
        "appworld_data_root": str(args.appworld_data_root.resolve()),
        "appworld_tasks_json": str(args.tasks.resolve()), "appworld_service_url": args.appworld_url,
        "provider": {"base_url_env": "SH_BASEURL", "api_key_env": "SH_APIKEY", "model_env": "SH_MODEL",
            "provider_id": "h8-deepseek-v41-flash", "tokenizer_path": str(args.tokenizer.resolve()),
            "timeout_seconds": 180, "tool_schema_mode": "legacy", "trust_env": False},
        "budget": {"input_tokens": 1000000, "output_tokens": 200000, "total_tokens": 1200000,
                   "calls": 80, "seconds": 1200},
        "physical_slots": 1, "repetitions": 3, "seed": 20260922,
        "solver": {"kind": "up-aries", "limits": {"timeout_seconds": 30, "max_expansions": None, "max_tokens": 131072}},
        "recursion_fuel": {"code-v1": 8, "appworld-v1": 8, "drone-sim-v1": 8},
        "context_policy": {"max_input_tokens": 65536, "max_total_tokens": 73728, "output_reserve": 8192,
                           "safety_margin": 512, "render_slack_tokens": 0},
        "orchestrator": {"max_concurrency": 2, "default_max_output_tokens": 8192,
            "max_output_tokens_ceiling": 16384, "max_model_calls_per_turn": 20, "max_tool_calls_per_turn": 40,
            "turn_deadline_seconds": 600, "test_timeout_seconds": 30,
            "attempt_reserve_tokens": 80000, "planner_reserve_tokens": 40000,
            "critic_reserve_tokens": 40000, "manager_reserve_tokens": 40000},
        "extra_input_reserve_tokens": 1024,
        "completion_contracts": {"code-v1": {"mode": "CONTENT_ONLY", "content_criterion_ids": "ALL_ROOT_CONTENT", "effects": []},
            "drone-sim-v1": {"mode": "CONTENT_ONLY", "content_criterion_ids": "ALL_ROOT_CONTENT", "effects": []},
            "appworld-v1": {"mode": "REQUIRED_EFFECTS", "by_task": contracts}}}
    output = write_evidence(root, "deployment.json", config)
    print(json.dumps({"config": str(root / output.relative_path), "sha256": output.sha256, "episodes_executed": 0}))


if __name__ == "__main__":
    main()
