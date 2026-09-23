"""Real final grader sees hidden tests without modifying the model workspace."""
import asyncio
import json
import pytest
from agent_orchestrator.evaluation.htn_oracles import grade_code
from agent_orchestrator.evaluation.htn_matrix import ScenarioDefinition
from agent_orchestrator.planning.htn.cross_domain_acceptance import ScenarioKind
from agent_orchestrator.runtime.sandbox import ProcessOnlyExecutor


@pytest.mark.parametrize("implementation,expected", [("return value * 2", True), ("return 2", False)])
def test_hidden_oracle_runs_only_after_execution_in_separate_directory(tmp_path, implementation, expected):
    workspace = tmp_path / "worker"
    (workspace / "tests").mkdir(parents=True)
    public = "from target import double\ndef test_public():\n    assert double(1) == 2\n"
    hidden = "from target import double\ndef test_hidden():\n    assert double(5) == 10\n"
    (workspace / "target.py").write_text(f"def double(value):\n    {implementation}\n")
    (workspace / "tests/test_target.py").write_text(public)
    root = tmp_path / "evidence"
    root.mkdir()
    scenario = ScenarioDefinition("hidden-oracle", "code-v1", ScenarioKind.NORMAL, {"goal": "double the input"},
        {"kind": "isolated_pytest", "immutable_files": {"tests/test_target.py": public},
         "hidden_files": {"tests/test_hidden.py": hidden},
         "test_paths": ["tests/test_target.py", "tests/test_hidden.py"], "timeout_seconds": 30})
    passed, ref = asyncio.run(grade_code(scenario, workspace, root, executor=ProcessOnlyExecutor()))
    assert passed is expected
    assert not (workspace / "tests/test_hidden.py").exists()
    assert (workspace / "tests/test_target.py").read_text() == public
    assert json.loads((root / ref.relative_path).read_text())["hidden_oracle_unchanged"]
