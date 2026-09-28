# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-28 真机第四局：12 次步骤尝试里 8 次浪费在模型写发布申请单上。

用户决定：申请单由系统按用户在确认页选定的操作生成，模型只负责把文件内容做对。
另：分层步骤的要求是编号（c-user-5），此前每一步都看到了整个任务的全部操作。
"""

from __future__ import annotations

import json

from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.action_commits import check_candidate
from agent_orchestrator.runtime.action_schema import (
    OPERATION_CANDIDATE_FILE,
    with_system_candidate,
    worker_action_contract,
)
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector

MISSION = [
    "README.md 引用 top_words",
    "action:file_publish.publish:README.md",
    "action:file_publish.publish:wordfreq.py",
]
ENABLED = DeploymentPolicy(enabled_connectors=("file_publish",))


class _Workspace:
    def __init__(self, root):
        self.root = root

    def resolve(self, relative):
        return self.root / relative

    def read_text(self, relative):
        return (self.root / relative).read_text(encoding="utf-8")

    def write_text(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


def _contract(tmp_path, task_criteria):
    return worker_action_contract(
        mission_criteria=MISSION, task_criteria=task_criteria,
        task_outputs=[OPERATION_CANDIDATE_FILE],
        connectors={"file_publish": FilePublishConnector(tmp_path / "pub", tmp_path / "ledger")},
        deployment=ENABLED,
    )


def test_a_step_sees_only_its_own_operation_by_requirement_number(tmp_path):
    contract = _contract(tmp_path, ["c-user-1", "c-user-3"])
    assert [op["target"] for op in contract["operations"]] == ["wordfreq.py"]
    assert contract["system_writes_candidate"] is True
    assert "Do not write any file under actions/" in contract["notice"]
    # 一个步骤同时负责两个发布：不由系统代写，照旧交给模型
    both = _contract(tmp_path, ["c-user-2", "c-user-3"])
    assert len(both["operations"]) == 2 and "system_writes_candidate" not in both


def test_the_system_writes_the_candidate_and_drops_the_models(tmp_path):
    ws = _Workspace(tmp_path / "ws")
    ws.write_text("wordfreq.py", "def top_words(text, n): ...\n")
    ws.write_text("actions/publish_wordfreq.json", json.dumps({"connector": "file_publish"}))
    ws.write_text(OPERATION_CANDIDATE_FILE, "not json")
    contract = _contract(tmp_path, ["c-user-3"])

    listed = with_system_candidate(
        ["actions/publish_wordfreq.json", OPERATION_CANDIDATE_FILE, "notes.md"], ws, contract)

    assert listed == ["notes.md", "wordfreq.py", OPERATION_CANDIDATE_FILE]
    candidate = json.loads(ws.read_text(OPERATION_CANDIDATE_FILE))
    assert candidate["params"] == {"artifact_path": "wordfreq.py"}
    checked, _ = check_candidate(
        candidate, criteria=MISSION,
        connectors={"file_publish": FilePublishConnector(tmp_path / "pub", tmp_path / "ledger")},
        deployment=ENABLED,
    )
    assert (checked["operation"], checked["target"]) == ("publish", "wordfreq.py")


def test_the_steps_own_listed_file_wins_over_an_unlisted_workspace_file(tmp_path):
    """审阅 2026-09-28：工作区里同名的原始文件不能顶替这一步真正的产出。"""
    ws = _Workspace(tmp_path / "ws")
    ws.write_text("weekly/report.md", "旧的原始文件\n")
    ws.write_text("report.md", "这一步写的\n")
    contract = worker_action_contract(
        mission_criteria=["action:file_publish.publish:weekly/report.md"],
        task_criteria=["c-user-1"], task_outputs=[OPERATION_CANDIDATE_FILE],
        connectors={"file_publish": FilePublishConnector(tmp_path / "pub", tmp_path / "ledger")},
        deployment=ENABLED,
    )
    assert "'weekly/report.md'" in contract["notice"]  # 告诉模型确切路径

    with_system_candidate(["report.md"], ws, contract)

    assert json.loads(ws.read_text(OPERATION_CANDIDATE_FILE))["params"] == {"artifact_path": "report.md"}


def test_a_candidate_that_cannot_be_written_is_left_to_the_rule_check(tmp_path):
    ws = _Workspace(tmp_path / "ws")
    ws.write_text("wordfreq.py", "x\n")
    ws.resolve(OPERATION_CANDIDATE_FILE).mkdir(parents=True)  # 被做成目录
    contract = _contract(tmp_path, ["c-user-3"])
    assert with_system_candidate(["wordfreq.py"], ws, contract) == ["wordfreq.py"]


def test_nothing_is_written_when_the_file_to_publish_is_missing(tmp_path):
    ws = _Workspace(tmp_path / "ws")
    ws.root.mkdir()
    contract = _contract(tmp_path, ["c-user-3"])
    listed = with_system_candidate(["notes.md"], ws, contract)
    assert listed == ["notes.md"]
    assert not ws.resolve(OPERATION_CANDIDATE_FILE).exists()


def test_a_step_owning_two_publishes_gets_two_system_written_candidates(tmp_path):
    """2026-09-28 真机第五局：两个发布交给同一步，而一步只有一个申请单文件，永远交不齐。"""
    from agent_orchestrator.runtime.action_schema import candidate_file

    ws = _Workspace(tmp_path / "ws")
    ws.write_text("README.md", "说明\n")
    ws.write_text("wordfreq.py", "x\n")
    contract = worker_action_contract(
        mission_criteria=MISSION, task_criteria=["c-user-2", "c-user-3"],
        task_outputs=[candidate_file(0), candidate_file(1)],
        connectors={"file_publish": FilePublishConnector(tmp_path / "pub", tmp_path / "ledger")},
        deployment=ENABLED,
    )
    assert contract["system_writes_candidate"] is True
    assert contract["output_files"] == [OPERATION_CANDIDATE_FILE, "actions/action_candidate-2.json"]

    listed = with_system_candidate([], ws, contract)

    assert sorted(listed) == sorted(["README.md", "wordfreq.py", *contract["output_files"]])
    written = {json.loads(ws.read_text(path))["target"]: json.loads(ws.read_text(path))["params"]
               for path in contract["output_files"]}
    assert written == {"README.md": {"artifact_path": "README.md"},
                       "wordfreq.py": {"artifact_path": "wordfreq.py"}}


def test_a_step_declares_one_candidate_file_per_owned_operation(monkeypatch):
    from types import SimpleNamespace

    from agent_orchestrator.runtime import action_schema
    from agent_orchestrator.storage import htn_store

    port = SimpleNamespace(port_key=action_schema.OPERATION_CANDIDATE_PORT)
    monkeypatch.setattr(htn_store.HtnStore, "__init__", lambda self, store: None)
    monkeypatch.setattr(htn_store.HtnStore, "task_semantics_of",
                        lambda self, mission_id, task_id: SimpleNamespace(output_ports=[port]))
    store = SimpleNamespace(get_mission=lambda mission_id: SimpleNamespace(success_criteria=MISSION))

    def declared(criteria):
        task = SimpleNamespace(id="t", outputs=["notes.md"], success_criteria=criteria)
        return action_schema.declared_action_outputs(store, "m", task)

    assert declared(["c-user-2", "c-user-3"]) == (
        "notes.md", OPERATION_CANDIDATE_FILE, "actions/action_candidate-2.json")
    assert declared(["c-user-3"]) == ("notes.md", OPERATION_CANDIDATE_FILE)
    assert declared(["c-user-1"]) == ("notes.md", OPERATION_CANDIDATE_FILE)  # 照旧至少一个


def test_the_candidate_port_names_the_file_the_system_wrote():
    """审阅 2026-09-28：申请单端口必填；模型按提示不写申请单，就由系统认领。"""
    from types import SimpleNamespace

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.action_schema import OPERATION_CANDIDATE_PORT

    def claim(raw, ports=("delivery", OPERATION_CANDIDATE_PORT), new_mode=True):
        mode = SimpleNamespace(declared_output_ports_for=lambda m, t: [{"port": p} for p in ports])
        fake = SimpleNamespace(_new_mode=lambda mission: mode if new_mode else None)
        contract = {"system_writes_candidate": True, "output_files": [OPERATION_CANDIDATE_FILE]}
        Orchestrator._claim_system_candidate(
            fake, raw, SimpleNamespace(mission_id="m", task_id="t"), None, contract)
        return raw.get("outputs")

    assert claim({"artifacts": ["README.md", OPERATION_CANDIDATE_FILE],
                  "outputs": {"delivery": "README.md"}}) == {
        "delivery": "README.md", OPERATION_CANDIDATE_PORT: OPERATION_CANDIDATE_FILE}
    assert claim({"artifacts": [OPERATION_CANDIDATE_FILE]}) == {
        OPERATION_CANDIDATE_PORT: OPERATION_CANDIDATE_FILE}
    assert claim({"artifacts": ["README.md"]}) is None  # 系统没写成：不替它认领
    assert claim({"artifacts": [OPERATION_CANDIDATE_FILE]}, ports=("delivery",)) is None
    assert claim({"artifacts": [OPERATION_CANDIDATE_FILE]}, new_mode=False) is None


def test_a_numbered_step_that_owns_no_operation_is_told_nothing_about_candidates(tmp_path):
    """2026-09-28 真机第五局：写文件那一步（只负责内容要求）被退回"整个任务的两个发布"，
    屡次写申请单被规则检查退回，5 次尝试白费。"""
    assert _contract(tmp_path, ["c-user-1", "pytest: test_wordfreq.py"]) is None
    # 不带编号的旧式步骤：照旧退回整个任务的操作
    legacy = _contract(tmp_path, ["README.md 引用 top_words"])
    assert len(legacy["operations"]) == 2
