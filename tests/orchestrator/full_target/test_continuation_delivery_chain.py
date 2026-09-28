# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 2A upstream runs (2026-09-27): two desktop chain defects.

1. A continuation step (every input port is also an output port, e.g.
   ``desktop.continue-delivery``) delivers the next version of what it received,
   but only its own file reached the next step: in "slugify.py → test_slugify.py →
   NOTES.md" the third step had the tests but not the module, pytest failed on the
   import, the Mission failed.  Its delivery now carries the inputs its accepted
   Attempt was frozen with (real producer, artifact id, hash), nearest version wins.
2. A hierarchical primitive with the operation candidate port was never given the
   candidate contract (its Task declares no file outputs), so it invented a format
   outside ``actions/`` and the operation workspace found no candidate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.versioning import UpstreamInput
from agent_orchestrator.contracts.semantic_base import VersionedRef
from agent_orchestrator.orchestrator import hierarchical_dispatch as hd
from agent_orchestrator.orchestrator.taskgraph_dispatch import _intent_inputs
from agent_orchestrator.runtime.action_schema import OPERATION_CANDIDATE_FILE, OPERATION_CANDIDATE_PORT
from agent_orchestrator.storage.store import StoreError

SCHEMA = VersionedRef("desktop.workspace-outputs", 1, "a" * 64)
M = "mission-chain"


def _port(key):
    return SimpleNamespace(port_key=key, schema_ref=SCHEMA)


def _artifact(task, path, digest, artifact_id):
    return SimpleNamespace(id=artifact_id, mission_id=M, task_id=task, path=path, content_hash=digest)


class _Store:
    """Only the reads ``carried_inputs`` makes, over three accepted steps."""

    def __init__(self, *, tampered=False):
        self.artifacts = {
            "a-module": _artifact("t-module", "slugify.py", "1" * 64, "a-module"),
            "a-tests": _artifact("t-tests", "test_slugify.py", "2" * 64, "a-tests"),
        }
        self.tasks = {
            "t-module": SimpleNamespace(id="t-module", mission_id=M, accepted_result_id="r-module",
                                        accepted_artifacts=("a-module",)),
            "t-tests": SimpleNamespace(id="t-tests", mission_id=M, accepted_result_id="r-tests",
                                       accepted_artifacts=("a-tests",)),
        }
        module_input = UpstreamInput("t-module", "slugify.py", ("9" if tampered else "1") * 64, "a-module")
        self.results = {"r-tests": SimpleNamespace(envelope=SimpleNamespace(attempt_id="t-tests:attempt-1"))}
        self.intents = {"t-tests:attempt-1": SimpleNamespace(config={"inputs": [module_input.to_json()]})}

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_result(self, result_id):
        return self.results.get(result_id)

    def get_intent_for_subject(self, subject):
        return self.intents.get(subject)

    def get_artifact(self, artifact_id):
        return self.artifacts.get(artifact_id)


@pytest.fixture
def continuation(monkeypatch):
    kinds = {"t-tests": (_port("delivery"),), "t-module": ()}

    class _Htn:
        def __init__(self, store):
            pass

        def task_semantics_of(self, mission_id, task_id):
            inputs = kinds.get(task_id)
            if inputs is None:
                return None
            return SimpleNamespace(input_ports=inputs, output_ports=(_port("delivery"),))

    monkeypatch.setattr(hd, "HtnStore", _Htn)


def test_a_continuation_producer_carries_what_it_received(continuation):
    """**Mutation**: return ``overlaid`` without the carried entries → red."""
    store = _Store()
    assert hd.carried_inputs(store, M, "t-tests") == [
        UpstreamInput("t-module", "slugify.py", "1" * 64, "a-module")]
    # a preparation producer (no input ports) carries nothing
    assert hd.carried_inputs(store, M, "t-module") == []


def test_a_tampered_or_unaccepted_carried_entry_is_not_carried(continuation):
    assert hd.carried_inputs(_Store(tampered=True), M, "t-tests") == []
    store = _Store()
    store.tasks["t-module"].accepted_artifacts = ()
    assert hd.carried_inputs(store, M, "t-tests") == []


def _intent(inputs, data_inputs):
    frozen = {"version": 2, "source_revision": 1, "manifest_hash": "m" * 64,
              "target_rules": None, "data_inputs": [item.to_json() for item in data_inputs]}
    message = {"text": "x"}
    from agent_orchestrator.contracts.models import sha256_hex
    return SimpleNamespace(config={"taskgraph_inputs": frozen, "inputs": [i.to_json() for i in inputs],
                                   "message": message}, input_hash=sha256_hex(message))


def test_the_frozen_check_accepts_the_carried_closure_and_nothing_else(monkeypatch):
    """**Mutation**: drop the ``closure`` allowance → the first assertion goes red."""
    from agent_orchestrator.orchestrator import taskgraph_dispatch as td

    direct = UpstreamInput("t-tests", "test_slugify.py", "2" * 64, "a-tests")
    carried = UpstreamInput("t-module", "slugify.py", "1" * 64, "a-module")
    monkeypatch.setattr(td, "decode_target_rules", lambda raw: None)
    monkeypatch.setattr(td, "manifest_upstream_inputs", lambda manifest, rules, network=None: [direct])
    both = [carried, direct]
    got = _intent_inputs(_intent(both, both), None, source_revision=1, manifest_hash="m" * 64,
                         carried=lambda producer: [carried] if producer == "t-tests" else [])
    assert list(got) == both
    stranger = UpstreamInput("t-other", "evil.py", "3" * 64, "a-other")
    with pytest.raises(StoreError, match="PRODUCER_MISMATCH"):
        _intent_inputs(_intent([stranger, direct], [stranger, direct]), None, source_revision=1,
                       manifest_hash="m" * 64, carried=lambda producer: [carried])


def test_the_operation_candidate_port_brings_the_candidate_contract(monkeypatch):
    """**Mutation**: return ``task.outputs`` unchanged → red."""
    from agent_orchestrator.orchestrator import event_handler as eh
    import agent_orchestrator.storage.htn_store as htn

    ports = {"with": (_port("delivery"), _port(OPERATION_CANDIDATE_PORT)), "without": (_port("delivery"),)}

    class _Htn:
        def __init__(self, store):
            pass

        def task_semantics_of(self, mission_id, task_id):
            return SimpleNamespace(output_ports=ports[task_id])

    monkeypatch.setattr(htn, "HtnStore", _Htn)
    # 一步负责几个操作就声明几个申请单文件（2026-09-28），此处任务没有操作要求：照旧一个
    fake = SimpleNamespace(store=SimpleNamespace(
        get_mission=lambda mission_id: SimpleNamespace(success_criteria=())))
    mission = SimpleNamespace(id=M)
    outputs = eh.Orchestrator._action_candidate_outputs

    def task(task_id, declared=()):
        return SimpleNamespace(id=task_id, outputs=declared, success_criteria=())

    assert outputs(fake, mission, task("with")) == (OPERATION_CANDIDATE_FILE,)
    assert outputs(fake, mission, task("without")) == ()
    declared = ("actions/mine.json",)
    assert outputs(fake, mission, task("with", declared)) == declared
    assert OPERATION_CANDIDATE_FILE.startswith("actions/") and OPERATION_CANDIDATE_FILE.endswith(".json")


# 旧式任务（不在完成协议下）：申请单仍由步骤自己写，规则检查照旧（2026-09-29 起完成协议下
# 申请单由系统按已批准效果生成，步骤写的一律忽略）。
_LEGACY_STORE = SimpleNamespace(connection=SimpleNamespace(
    execute=lambda *args: SimpleNamespace(fetchone=lambda: None)))


def test_the_rule_check_accepts_the_candidate_the_contract_asked_for(tmp_path):
    """opt.35 真机：候选按说明写到 actions/action_candidate.json，规则检查却因任务输出列表
    为空判"未声明"，同一步重试。规则检查与候选说明必须用同一份声明。

    **Mutation**: check ``task.outputs`` again → red."""
    from agent_orchestrator.orchestrator import event_handler as eh

    (tmp_path / "actions").mkdir()
    (tmp_path / OPERATION_CANDIDATE_FILE).write_text("{}", encoding="utf-8")
    fake = SimpleNamespace(
        commit=SimpleNamespace(domain_for=lambda mission_id: SimpleNamespace(id="code-v1")),
        store=_LEGACY_STORE,
        _action_candidate_outputs=lambda mission, task: (OPERATION_CANDIDATE_FILE,),
        _connectors={}, _config=SimpleNamespace(deployment_policy=None),
    )
    problems = eh.Orchestrator._action_problems(
        fake, SimpleNamespace(id=M, success_criteria=()), SimpleNamespace(outputs=(), success_criteria=()),
        [SimpleNamespace(path=OPERATION_CANDIDATE_FILE)], SimpleNamespace(resolve=lambda p: tmp_path / p))
    assert problems is not None
    assert not [p for p in problems if "not a declared output" in p]


def test_the_accept_transaction_uses_the_same_declaration(monkeypatch):
    """opt.36 真机：规则检查放行后，验收事务里第三处读 ``task.outputs`` 的检查又以
    ``undeclared_action_output`` 拒绝同一个候选。

    **Mutation**: check ``task.outputs`` in ``_action_candidates`` again → red."""
    from agent_orchestrator.orchestrator import action_commits as ac
    import agent_orchestrator.runtime.action_schema as schema

    import agent_orchestrator.artifacts.store as artifact_store

    monkeypatch.setattr(schema, "declared_action_outputs",
                        lambda store, mission_id, task: (OPERATION_CANDIDATE_FILE,))
    reached = []

    def read(artifact):
        reached.append(artifact.path)
        raise artifact_store.ArtifactStoreError("stub", artifact.path)

    monkeypatch.setattr(artifact_store, "read_verified", read)
    artifact = SimpleNamespace(id="a1", path=OPERATION_CANDIDATE_FILE, content_hash="0" * 64)
    fake = SimpleNamespace(_store=SimpleNamespace(get_artifact=lambda artifact_id: artifact))
    found, rejected = ac.ActionCommitsMixin._action_candidates(
        fake, SimpleNamespace(artifacts=("a1",)), SimpleNamespace(outputs=()), SimpleNamespace(id=M),
        connectors={}, deployment=None)
    assert reached == [OPERATION_CANDIDATE_FILE]  # got past the declaration check
    assert "undeclared_action_output" not in str(rejected)


def test_the_published_artifact_is_verified_against_the_accepted_inputs(monkeypatch):
    """2A.1j：审阅员两次以"参数所指产物≠候选文件"误拒发布。确定性检查现在自己核对：参数要
    发布的产物必须是冻结已验收输入里的同一产物（id 与哈希一致），并把核对结果交给审阅员。

    **Mutation**: return a fact without checking membership → the stranger case goes red."""
    from agent_orchestrator.orchestrator import operation_proposal_review as review
    import agent_orchestrator.storage.htn_store as htn

    accepted = SimpleNamespace(id="artifact-notes", content_hash="5" * 64)

    class _Htn:
        def __init__(self, store):
            pass

        def get_acceptance(self, acceptance_id):
            return SimpleNamespace(artifact_refs=(accepted,))

    monkeypatch.setattr(htn, "HtnStore", _Htn)

    def payloads(artifact_id, digest):
        return SimpleNamespace(parameters=SimpleNamespace(
            effective_params={"artifact_id": artifact_id, "artifact_path": "NOTES.md", "content_hash": digest},
            accepted_input_refs=(SimpleNamespace(id="acc-1"),)))

    fact = review._published_artifact(None, payloads("artifact-notes", "5" * 64))
    assert fact["in_accepted_inputs"] is True and fact["accepted_by"] == "acc-1"
    with pytest.raises(review.ActionProposalReviewError, match="not an accepted input"):
        review._published_artifact(None, payloads("artifact-stranger", "5" * 64))
    with pytest.raises(review.ActionProposalReviewError, match="hash differs"):
        review._published_artifact(None, payloads("artifact-notes", "6" * 64))
    assert review._published_artifact(None, SimpleNamespace(parameters=SimpleNamespace(
        effective_params={}, accepted_input_refs=()))) is None



def test_the_mission_judge_view_keeps_tool_authority_while_its_mission_is_live():
    """opt.38 真机：根结论已提交后，任务终判评审员读 NOTES.md 等文件全部被拒
    （``attempt_unavailable``——它的工作区是"<任务>-judge-<所有者>"视图，不是执行尝试），
    于是判"不满足"，任务失败。

    **Mutation**: return ``attempt_unavailable`` for every non-Attempt id again → red."""
    import contextlib

    from agent_orchestrator.contracts.models import MissionStatus
    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    missions = {"mission-live": SimpleNamespace(status=MissionStatus.ACTIVE),
                "mission-done": SimpleNamespace(status=MissionStatus.COMPLETED)}
    store = SimpleNamespace(read_view=contextlib.nullcontext, get_attempt=lambda attempt_id: None,
                            get_mission=missions.get)
    fake = SimpleNamespace(store=store)
    refusal = Orchestrator._tool_execution_refusal
    assert refusal(fake, "mission-live-judge-owner-1") is None
    assert refusal(fake, "mission-done-judge-owner-1") == "attempt_unavailable"
    assert refusal(fake, "task-x:attempt-9") == "attempt_unavailable"


def test_an_undeclared_file_under_actions_is_policed_only_when_it_claims_an_action(tmp_path):
    """2026-09-27 真机（ABS.md 那局）：发布步骤把交付说明写成 actions/delivery.json，
    被当成"未声明的候选"连拒三次、任务失败。只有自称是操作（带 connector/operation）的
    文件才按候选把关；读不懂的照旧拒绝。

    **Mutation**: drop the ``claims_an_action`` skip → the delivery note is refused again."""
    import json

    from agent_orchestrator.orchestrator import event_handler as eh
    from agent_orchestrator.orchestrator.action_commits import claims_an_action

    (tmp_path / "actions").mkdir()
    (tmp_path / "actions" / "delivery.json").write_text(
        json.dumps({"delivered": "ABS.md"}), encoding="utf-8")
    (tmp_path / "actions" / "sneaky.json").write_text(
        json.dumps({"connector": "file_publish", "operation": "publish"}), encoding="utf-8")
    fake = SimpleNamespace(
        commit=SimpleNamespace(domain_for=lambda mission_id: SimpleNamespace(id="code-v1")),
        store=_LEGACY_STORE,
        _action_candidate_outputs=lambda mission, task: (OPERATION_CANDIDATE_FILE,),
        _connectors={}, _config=SimpleNamespace(deployment_policy=None),
    )

    def problems_for(path):
        return eh.Orchestrator._action_problems(
            fake, SimpleNamespace(id=M, success_criteria=()),
            SimpleNamespace(outputs=(), success_criteria=()),
            [SimpleNamespace(path=path)], SimpleNamespace(resolve=lambda p: tmp_path / p))

    assert problems_for("actions/delivery.json") == []
    assert [p for p in problems_for("actions/sneaky.json") if "not a declared output" in p]
    assert claims_an_action(b"{not json") and claims_an_action(b"[1]")
    assert not claims_an_action(b'{"note": "x"}')
