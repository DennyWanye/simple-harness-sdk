"""I08: original admission producers and Commit work without NanoJev imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


_CHILD = r'''
import asyncio, importlib.abc, json, sys
from pathlib import Path

# Fail on attempted dependency loading, rather than merely inspecting a flag.
class NoNanoJev(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if "nanojev" in fullname.lower():
            raise AssertionError("removed dependency requested: " + fullname)
sys.meta_path.insert(0, NoNanoJev())
root, checkout = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(checkout / "src"), str(checkout / "tests/orchestrator/full_target")]
from test_h1i_production_entry import _config, _seed_new_protocol, _refine_reply
from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
import agent_orchestrator.governance.planning_authorization as authorization
import agent_orchestrator.runtime.planning_operations as operations
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.plan_preview import CandidatePreview
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

async def main():
    counts = {"authorization": 0, "operation_snapshot": 0, "shape": 0, "commit": 0}
    originals = (authorization.build_planning_authorization, operations.build_operation_snapshot)
    def authority(*args, **kwargs):
        result = originals[0](*args, **kwargs)
        assert isinstance(result, authorization.PlanningAuthorizationSnapshot)
        counts["authorization"] += 1
        return result
    def operation(*args, **kwargs):
        result = originals[1](*args, **kwargs)
        counts["operation_snapshot"] += 1
        return result
    authorization.build_planning_authorization = authority
    operations.build_operation_snapshot = operation
    provider = RoleScriptedProvider({"planner": []})
    async with Orchestrator(_config(root), provider) as loop:
        mission, _, _, dispatch = _seed_new_protocol(loop, root, key="h1h-i08")
        original_preview = dispatch.preview_plan_proposal
        def preview(*args, **kwargs):
            result = original_preview(*args, **kwargs)
            assert isinstance(result, CandidatePreview)
            counts["shape"] += 1
            return result
        dispatch.preview_plan_proposal = preview
        original_commit = loop.commit.commit_planning_revision
        def commit(*args, **kwargs):
            result = original_commit(*args, **kwargs)
            counts["commit"] += 1
            return result
        loop.commit.commit_planning_revision = commit
        with loop.store.transaction():
            intent = loop._create_planner_intent_now(mission.id, ordinal=1)
            PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id,
                principal=Principal("i08-user")).issue(mission.id, command_id="i08-grant",
                    request_id=intent.intent_id, planner_principal_id=loop._owner)
        await loop._collect_plan_decision(intent, object(), mission,
            _refine_reply(intent.config["planning_package"]), dispatch)
        assert all(value > 0 for value in counts.values()), counts
        assert counts["shape"] == counts["commit"] == 1
        assert dispatch.network(mission.id).plan_revision == 1
        events = loop.store.list_events(mission.id)
        assert len([e for e in events if e.type == "PlanRevisionCommitted"]) == 1
        assert not [e for e in events if "shadow" in e.type.lower() or "nanojev" in e.type.lower()]
        assert not [name for name in sys.modules if "nanojev" in name.lower()]
        print(json.dumps({"counts": counts, "revision": 1, "provider_calls": 0}))
asyncio.run(main())
'''


def test_i08_no_nanojev_process_executes_three_producers_and_original_commit(tmp_path):
    checkout = Path(__file__).resolve().parents[3]
    # No credentials, deployment flags or application-specific environment survive.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD, str(tmp_path), str(checkout)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 0, (child.stdout, child.stderr)
    result = json.loads(child.stdout.splitlines()[-1])
    assert result["revision"] == 1
    assert result["counts"]["commit"] == 1
