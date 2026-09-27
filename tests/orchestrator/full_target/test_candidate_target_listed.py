"""The file an operation candidate names is listed in the step's own Result (2A)."""

from __future__ import annotations

import json

from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.runtime.action_schema import with_candidate_targets


def _workspace(tmp_path, candidate):
    (tmp_path / "actions").mkdir()
    (tmp_path / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    (tmp_path / "actions" / "action_candidate.json").write_text(
        candidate if isinstance(candidate, str) else json.dumps(candidate), encoding="utf-8"
    )
    return Workspace(root=tmp_path, attempt_id="a1", writable=True)


def _candidate(path):
    return {"connector": "file_publish", "operation": "publish", "target": "NOTES.md",
            "params": {"artifact_path": path}}


def test_an_unchanged_received_file_is_listed_for_the_worker(tmp_path):
    ws = _workspace(tmp_path, _candidate("NOTES.md"))
    listed = ["actions/action_candidate.json", "delivery.json"]
    assert with_candidate_targets(listed, ws) == listed + ["NOTES.md"]


def test_an_already_listed_file_is_not_repeated(tmp_path):
    ws = _workspace(tmp_path, _candidate("NOTES.md"))
    listed = ["NOTES.md", "actions/action_candidate.json"]
    assert with_candidate_targets(listed, ws) == listed


def test_nothing_is_invented(tmp_path):
    listed = ["actions/action_candidate.json"]
    for path in ("MISSING.md", "../outside.md", "actions/other.json", "", 7):
        assert with_candidate_targets(listed, _fresh(tmp_path, _candidate(path))) == listed
    assert with_candidate_targets(listed, _fresh(tmp_path, "{not json")) == listed
    assert with_candidate_targets(listed, _fresh(tmp_path, {"params": "x"})) == listed
    # a candidate that is not listed is not read at all
    assert with_candidate_targets(["delivery.json"], _fresh(tmp_path, _candidate("NOTES.md"))) == [
        "delivery.json"]


_n = [0]


def _fresh(tmp_path, candidate):
    _n[0] += 1
    root = tmp_path / f"w{_n[0]}"
    root.mkdir()
    return _workspace(root, candidate)
