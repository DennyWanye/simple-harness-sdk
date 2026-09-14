"""Host-only isolated official evaluator. Never registered as an Agent tool."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _declared_status(task_id: str, experiment_name: str) -> str:
    """Read the saved public supervisor state, with no extra Agent/world action."""
    from importlib import import_module

    paths = import_module("appworld.common.path_store").path_store
    database = import_module("appworld.apps.model_lib").get_db_home_path
    collection = import_module("appworld.collections.models").ModelCollection.load(
        from_db_home_path=str(
            Path(paths.experiment_outputs) / experiment_name / "tasks" / task_id / "dbs"
        ),
        to_db_home_path=database(storage_type="memory", type="task_output", task_id=task_id),
        load_apps=["supervisor"],
    )
    try:
        tasks = collection.supervisor.Task.all()
        if len(tasks) != 1:
            raise ValueError("expected one saved supervisor task")
        status = tasks[0].status
        if status not in {None, "success", "fail"}:
            raise ValueError("unknown supervisor task status")
        return "pending" if status is None else str(status)
    finally:
        collection.close()


def main() -> None:
    from importlib import import_module

    update_root = import_module("appworld").update_root
    evaluate_task = import_module("appworld.evaluator").evaluate_task

    request = json.load(sys.stdin)
    update_root(request["root"])
    status = _declared_status(request["task_id"], request["experiment_name"])
    result = evaluate_task(
        task_id=request["task_id"], experiment_name=request["experiment_name"], save_report=False
    )
    payload = result.to_dict()
    payload["declared_task_status"] = status
    print(json.dumps(payload, allow_nan=False))


if __name__ == "__main__":
    main()
