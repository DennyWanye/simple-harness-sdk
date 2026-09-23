#!/usr/bin/env python3
"""Freeze and evaluate H6 against real Missions in one orchestrator SQLite store.

This runner never creates Missions, inserts usage, manufactures reviews, or calls a
Provider.  The normal hierarchical runtime must execute every named Mission in the
same database.  That separation is intentional: freeze the cohort before scheduling
trial/heldout work, run those Missions through the original runtime, then evaluate.

Cohort JSON::

    {
      "domain_id": "code-v1",
      "trial_mission_ids": ["... exactly 20 or more ..."],
      "heldout_mission_ids": ["... exactly 5 or more ..."],
      "baseline_mission_ids": ["... one or more independent Missions ..."]
    }

Examples (the commands print only IDs, states, counts, and evidence hashes)::

    .venv/bin/python scripts/acceptance/run_h6_method_evaluation.py freeze \
      --db .local-test-evidence/2026-09-22/h6/orchestrator.db \
      --method-id code.method --method-version 1 --method-hash <sha256> \
      --cohort .local-test-evidence/2026-09-22/h6/cohort.json

    .venv/bin/python scripts/acceptance/run_h6_method_evaluation.py evaluate \
      --db .local-test-evidence/2026-09-22/h6/orchestrator.db \
      --method-id code.method --method-version 1 --method-hash <sha256>

``evaluate`` is fail closed.  Production ``MethodEvaluationStore`` re-reads terminal
Missions, adopted method instances, official root reviews, actions, imported usage,
and unsettled reservations before deriving the record.  Use ``promote`` only after a
passing evaluation when the registry transition itself is part of acceptance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestrator.contracts.htn import MethodRef  # noqa: E402
from agent_orchestrator.planning.htn.method_lifecycle import (  # noqa: E402
    EvaluationSetV1,
    MethodLifecyclePolicyV1,
)
from agent_orchestrator.storage.method_evaluation_store import (  # noqa: E402
    MethodEvaluationStore,
)
from agent_orchestrator.storage.store import Store  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze/evaluate H6 from real durable Mission evidence; makes no provider calls."
    )
    parser.add_argument("command", choices=("freeze", "evaluate", "promote", "inspect"))
    parser.add_argument("--db", type=Path, required=True, help="existing orchestrator SQLite database")
    parser.add_argument("--method-id", required=True)
    parser.add_argument("--method-version", type=int, required=True)
    parser.add_argument("--method-hash", required=True, help="exact registered method SHA-256")
    parser.add_argument("--cohort", type=Path, help="required by freeze; JSON schema is in this docstring")
    args = parser.parse_args()
    if args.command == "freeze" and args.cohort is None:
        parser.error("freeze requires --cohort")
    if args.command != "freeze" and args.cohort is not None:
        parser.error("--cohort is accepted only by freeze")
    if args.method_version < 1:
        parser.error("--method-version must be positive")
    if len(args.method_hash) != 64:
        parser.error("--method-hash must be a SHA-256 hex digest")
    try:
        int(args.method_hash, 16)
    except ValueError:
        parser.error("--method-hash must be a SHA-256 hex digest")
    return args


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{name} must be a non-empty JSON string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must contain unique Mission IDs")
    return result


def _cohort(path: Path) -> tuple[EvaluationSetV1, tuple[str, ...]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "domain_id", "trial_mission_ids", "heldout_mission_ids", "baseline_mission_ids"
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError(f"cohort must contain exactly {sorted(expected)}")
    domain = raw["domain_id"]
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("domain_id must be non-empty text")
    trials = _strings(raw["trial_mission_ids"], "trial_mission_ids")
    heldout = _strings(raw["heldout_mission_ids"], "heldout_mission_ids")
    baselines = _strings(raw["baseline_mission_ids"], "baseline_mission_ids")
    policy = MethodLifecyclePolicyV1()
    if len(trials) < policy.min_trials or len(heldout) < policy.min_heldout:
        raise ValueError(
            f"default H6 policy requires at least {policy.min_trials} trials and "
            f"{policy.min_heldout} heldout Missions"
        )
    all_ids = (*trials, *heldout, *baselines)
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("trial, heldout, and baseline Mission IDs must be mutually disjoint")
    return EvaluationSetV1.build(domain, trials, heldout), baselines


def _inspection(store: Store, service: MethodEvaluationStore, reference: MethodRef) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT frozen_json,state,evaluation_json,evidence_hash FROM method_evaluations "
        "WHERE method_id=? AND method_version=?",
        (reference.method_id, reference.version),
    ).fetchone()
    if row is None or json.loads(row["frozen_json"])["method_ref"] != reference.to_json():
        raise ValueError("method has no exact frozen evaluation in this database")
    frozen = json.loads(row["frozen_json"])
    evaluation_set = EvaluationSetV1.from_json(frozen["evaluation_set"])
    cohorts = {
        "trial": evaluation_set.trial_ids,
        "heldout": evaluation_set.heldout_ids,
        "baseline": tuple(frozen["baseline_mission_ids"]),
    }
    runs: list[dict[str, Any]] = []
    for cohort, mission_ids in cohorts.items():
        for mission_id in mission_ids:
            mission = store.get_mission(mission_id)
            usage = store.connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(unknown),0) FROM imported_usage WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            unsettled = store.connection.execute(
                "SELECT COUNT(*) FROM budget_reservations WHERE mission_id=? AND state!='SETTLED'",
                (mission_id,),
            ).fetchone()[0]
            instances = service.htn.list_method_instances(mission_id)
            resolutions = service.htn.list_goal_resolutions(mission_id)
            runs.append({
                "cohort": cohort,
                "mission_id": mission_id,
                "mission_status": None if mission is None else str(mission.status),
                "exercised_method": (
                    None if cohort == "baseline" else any(i.method_ref == reference for i in instances)
                ),
                "method_instances": len(instances),
                "goal_resolutions": len(resolutions),
                "usage_receipts": int(usage[0]),
                "unknown_usage_receipts": int(usage[1]),
                "unsettled_reservations": int(unsettled),
            })
    return {
        "method_ref": reference.to_json(),
        "evaluation_state": str(row["state"]),
        "evaluation_set_hash": evaluation_set.content_hash,
        "evidence_hash": row["evidence_hash"],
        "has_evaluation_record": row["evaluation_json"] is not None,
        "counts": {name: len(ids) for name, ids in cohorts.items()},
        "runs": runs,
    }


def main() -> int:
    args = _arguments()
    if not args.db.is_file():
        print(json.dumps({"status": "BLOCKED", "reason": "database_not_found"}))
        return 2
    reference = MethodRef(args.method_id, args.method_version, args.method_hash)
    store = Store.open(args.db)
    try:
        service = MethodEvaluationStore(store)
        if args.command == "freeze":
            evaluation_set, baselines = _cohort(args.cohort)
            frozen = service.freeze(reference, evaluation_set, baseline_mission_ids=baselines)
            output = {
                "status": "FROZEN",
                "method_ref": frozen["method_ref"],
                "evaluation_set_hash": frozen["evaluation_set"]["content_hash"],
                "counts": {
                    "trial": len(evaluation_set.trial_ids),
                    "heldout": len(evaluation_set.heldout_ids),
                    "baseline": len(baselines),
                },
            }
        elif args.command == "inspect":
            output = {"status": "INSPECTED", **_inspection(store, service, reference)}
        elif args.command == "evaluate":
            result = service.evaluate(reference)
            output = {"status": str(result["state"]), **result}
        else:
            admitted = service.promote(reference)
            output = {
                "status": str(admitted.status),
                "method_ref": admitted.method_ref.to_json(),
                "admission_receipt_ref": (
                    None if admitted.admission_receipt_ref is None
                    else admitted.admission_receipt_ref.to_json()
                ),
            }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({
            "status": "BLOCKED",
            "error_type": type(error).__name__,
            "reason": str(error)[:500],
        }, ensure_ascii=False, sort_keys=True))
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
