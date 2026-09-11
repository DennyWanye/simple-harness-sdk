# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · slice E (D7-10 / D7-11): ``demo --scenario approval-action`` end to end on
fixtures — candidate, approval, hand-off and receipt in one evidence directory — and the
``approval`` CLI a person uses between two runs."""

from __future__ import annotations

import json

from agent_orchestrator.__main__ import main
from agent_orchestrator.observability.secrets import find_secrets


def _events(evidence):
    return [
        (lambda e: e.get("type") or e.get("event_type"))(json.loads(line))
        for line in (evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _demo(evidence, key, *extra):
    return main(
        [
            "demo",
            "--scenario",
            "approval-action",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            key,
            *extra,
        ]
    )


def test_the_approval_action_demo_shows_the_whole_chain(tmp_path, capsys):
    evidence = tmp_path / "s7"
    assert _demo(evidence, "s7-demo") == 0
    capsys.readouterr()
    report = json.loads((evidence / "test-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "COMPLETED" and report["approved_in_this_run"]
    [action] = report["chain"]["actions"]
    assert action["state"] == "SUCCEEDED" and action["level"] == "L2" and action["handoffs"] == 1
    assert (
        len(action["decision_receipts"]) == 1
        and action["receipt_hash"]
        and action["candidate_artifact_id"]
    )
    assert report["chain"]["service"]["config"] == {"feature_flags.new_ui": "on"}
    assert report["chain"]["service"]["applied_count"] == 1
    for name in (
        "actions.json",
        "approvals.json",
        "trace.json",
        "metrics.json",
        "events.jsonl",
        "final_state.json",
    ):
        assert (evidence / name).is_file(), name
    approvals = json.loads((evidence / "approvals.json").read_text(encoding="utf-8"))
    [request] = approvals["requests"]
    [decision] = approvals["decisions"][request["request_id"]]
    assert request["state"] == "GRANTED" and decision["principal_id"] == "demo-operator"
    assert decision["receipt_hash"] == action["decision_receipts"][0]
    types = _events(evidence)
    order = [
        "ActionProposed",
        "ApprovalRequested",
        "ApprovalGranted",
        "ActionHandedOff",
        "ActionSucceeded",
        "MissionCompleted",
    ]
    assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)
    trace = json.loads((evidence / "trace.json").read_text(encoding="utf-8"))
    assert trace["actions"][0]["receipt_hash"] == action["receipt_hash"]
    metrics = json.loads((evidence / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["human"]["decisions"] == 1 and metrics["actions"]["handoffs"] == 1
    for path in evidence.rglob("*.json*"):
        assert find_secrets(path.read_text(encoding="utf-8")) == [], path


def test_a_person_approves_from_the_cli_between_two_runs(tmp_path, capsys):
    evidence = tmp_path / "s7"
    assert _demo(evidence, "s7-pause", "--pause-for-approval") == 4  # waiting for a person
    capsys.readouterr()
    cli = ["--evidence-dir", str(evidence)]
    assert main(["approval", "list", *cli, "--as", "alice"]) == 0
    [request] = json.loads(capsys.readouterr().out)
    assert request["kind"] == "action" and request["action"]["target"] == "feature_flags.new_ui"
    assert request["action"]["reason"]["source"] == "model (untrusted)"
    assert (
        main(
            [
                "approval",
                "comment",
                request["request_id"],
                *cli,
                "--as",
                "bob",
                "--text",
                "周四上线窗口",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "approval",
                "comment",
                request["request_id"],
                *cli,
                "--as",
                "bob",
                "--text",
                "key sk-" + "b" * 40,
            ]
        )
        == 1
    )
    assert (
        main(
            ["approval", "approve", request["request_id"], *cli, "--as", "alice", "--nonce", "n-1"]
        )
        == 0
    )
    capsys.readouterr()
    assert _demo(evidence, "s7-pause") == 0  # the same key continues the same Mission
    capsys.readouterr()
    report = json.loads((evidence / "test-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "COMPLETED" and report["approved_in_this_run"] == []
    approvals = json.loads((evidence / "approvals.json").read_text(encoding="utf-8"))
    [request] = approvals["requests"]
    assert [c["text"] for c in request["comments"]] == ["周四上线窗口"]
    assert [d["principal_id"] for d in approvals["decisions"][request["request_id"]]] == ["alice"]


def test_a_rejection_from_the_cli_changes_nothing_and_fails_the_mission(tmp_path, capsys):
    evidence = tmp_path / "s7"
    assert _demo(evidence, "s7-reject", "--pause-for-approval") == 4
    capsys.readouterr()
    cli = ["--evidence-dir", str(evidence), "--as", "alice"]
    assert main(["approval", "list", *cli]) == 0
    [request] = json.loads(capsys.readouterr().out)
    assert (
        main(["approval", "reject", request["request_id"], *cli, "--reason", "不在变更窗口"]) == 0
    )
    assert main(["approval", "approve", request["request_id"], *cli]) == 1  # closed
    capsys.readouterr()
    assert _demo(evidence, "s7-reject") == 1
    capsys.readouterr()
    report = json.loads((evidence / "test-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAILED" and report["stop_reason"] == "approval_rejected"
    assert (
        report["chain"]["service"]["config"] == {}
        and report["chain"]["service"]["applied_count"] == 0
    )
    task_id = json.loads((evidence / "final_state.json").read_text(encoding="utf-8"))["tasks"][0][
        "id"
    ]
    assert (
        main(["approval", "takeover", task_id, *cli, "--action", "stop", "--basis", "已结束"]) == 1
    )  # no revival
