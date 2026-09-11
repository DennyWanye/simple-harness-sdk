# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · slice F (plan D9-11'; S9-01…S9-07 end to end): ``demo --scenario
policy-promotion`` on fixtures — history → a traceable candidate → gates (one fails, one
passes and still only runs in evaluation libraries) → approval → promotion (a Mission
created before it keeps its version) → rollback (events and costs untouched) → a refusal
for too little history — and the ``policy`` commands a person uses on the result."""

from __future__ import annotations

import json
from pathlib import Path

from agent_orchestrator.__main__ import main
from agent_orchestrator.observability.replay import replay_mission


def _demo(root, capsys):
    code = main(
        [
            "demo",
            "--scenario",
            "policy-promotion",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(root),
            "--test-timeout",
            "60",
        ]
    )
    capsys.readouterr()
    return code, json.loads((root / "summary.json").read_text(encoding="utf-8"))


def test_the_policy_promotion_demo_closes_the_loop(tmp_path, capsys):
    root = Path(tmp_path) / "s9"
    code, summary = _demo(root, capsys)
    assert code == 0, summary
    assert summary["note"].startswith("机制验证")
    # S9-07: too little history is refused and nothing is registered for it
    assert summary["thin_history"]["outcome"] == "insufficient"
    # S9-01: the learner's candidate traces back to its training data, code and rules
    learned = summary["learned"]
    assert learned["outcome"] == "candidate" and learned["training_missions"] == 6
    assert learned["routing"] == {"code": "large"} and learned["learner"] == "rules-v1"
    # S9-02: a candidate that routes code to the flaky profile fails the gates
    assert summary["flaky"]["verdict"] == "FAILED" and summary["flaky"]["reasons"]
    # S9-03: the learned candidate passes, still only in evaluation libraries, not promotable unapproved
    assert summary["gate"]["verdict"] == "PASSED" and summary["gate"]["evidence_kind"] == "fixture"
    assert "APPROVED" in summary["unapproved_promotion"]
    # S9-04: bound at creation — the Mission created before the promotion keeps the seed
    before, after, later = (summary["missions"][k] for k in ("before", "after", "later"))
    seed, candidate = summary["versions"]["seed"], summary["versions"]["candidate"]
    assert before["policy_version_id"] == seed and before["status"] == "COMPLETED"
    assert (
        before["first_profile"] == "small"
    )  # the seed's routing, although the candidate was ACTIVE
    assert after["policy_version_id"] == candidate and after["first_profile"] == "large"
    # S9-05: rolled back at once; the rolled-back version's events and costs are untouched
    assert summary["rollback"]["version_id"] == seed and later["policy_version_id"] == seed
    assert summary["rollback"]["events_unchanged"] and summary["rollback"]["usage_unchanged"]
    assert summary["registry_consistency"] == []
    for mission in (before, after, later):
        report = replay_mission(
            mission_id=mission["mission_id"], library=root / "production" / "orchestrator.db"
        )
        assert report["comparison"]["mismatches"] == [] and report["comparison"]["coverage"] == 1.0
    kinds = [
        json.loads(line)["type"]
        for line in (root / "policy_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {
        "PolicySeeded",
        "PolicyProposed",
        "PolicyEvaluated",
        "PolicyApproved",
        "PolicyPromoted",
        "PolicyRolledBack",
        "PolicyProposalRefused",
    } <= set(kinds)
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert registry["consistency"] == [] and registry["activations"][-1]["action"] == "rollback"

    # the commands a person uses on the result
    prod = str(root / "production")
    assert main(["policy", "status", "--evidence-dir", prod]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active_version_id"] == seed and status["consistency"] == []
    assert main(["policy", "show", learned["proposal_id"], "--evidence-dir", prod]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["state"] == "PROMOTED" and shown["evaluations"] and shown["decisions"]
    params = Path(tmp_path) / "params.json"
    params.write_text(json.dumps({"no_progress_limit": 3}), encoding="utf-8")
    assert (
        main(
            [
                "policy",
                "propose",
                "--params",
                str(params),
                "--evidence-dir",
                prod,
                "--as",
                "bob",
                "--note",
                "放宽一次",
            ]
        )
        == 0
    )
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["source"] == "human:bob"
    assert (
        main(["policy", "approve", proposal["proposal_id"], "--evidence-dir", prod, "--as", "bob"])
        == 1
    )  # not evaluated
    assert (
        main(["policy", "promote", proposal["proposal_id"], "--evidence-dir", prod, "--as", "bob"])
        == 1
    )
    assert (
        main(
            [
                "policy",
                "rollback",
                "--to",
                candidate,
                "--evidence-dir",
                prod,
                "--as",
                "bob",
                "--reason",
                "回到被回滚的版本",
            ]
        )
        == 1
    )
    capsys.readouterr()  # the refusals above each printed their answer
    core = Path(tmp_path) / "core.json"
    core.write_text(json.dumps({"hard_cap_micros": 1}), encoding="utf-8")
    assert (
        main(["policy", "propose", "--params", str(core), "--evidence-dir", prod, "--as", "bob"])
        == 1
    )
    assert "not promotable" in json.loads(capsys.readouterr().out)["error"]
    assert (
        main(
            [
                "policy",
                "propose",
                "--history",
                str(root / "history"),
                "--evidence-dir",
                prod,
                "--as",
                "bob",
                "--min-missions",
                "10",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["outcome"] == "insufficient"
    assert main(["policy", "show", "no-such-thing", "--evidence-dir", prod]) == 1
    capsys.readouterr()


def test_review_p2_4_policy_reading_verbs_never_write_and_a_missing_library_is_an_error(
    tmp_path, capsys
):
    nowhere = Path(tmp_path) / "nowhere"
    for verb in ("list", "status"):
        assert main(["policy", verb, "--evidence-dir", str(nowhere)]) == 2
        assert "no orchestrator library" in json.loads(capsys.readouterr().out)["error"]
    assert not (nowhere / "orchestrator.db").exists()  # no empty library created by accident
