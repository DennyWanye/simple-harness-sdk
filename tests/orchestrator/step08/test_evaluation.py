# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · slice D (plan D8-6' / D8-7' / D8-8'; S8-03, S8-06): the same cases under two
strategies, independent trials, new Missions in new libraries, sample sizes and honest
comparisons; a hidden oracle measures verification misjudgment; a re-run of an old task
under a new model is a new Evaluation that leaves the old facts alone."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, proposal_step

from agent_orchestrator.__main__ import main
from agent_orchestrator.contracts import Budget
from agent_orchestrator.observability.evaluation import (
    EvaluationCase,
    EvaluationPlan,
    EvaluationRefused,
    Oracle,
    Strategy,
    _compare,
    _summary,
    case_from_evidence,
    fisher_exact,
    library_digest,
    run_plan,
    wilson,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.runtime.connectors import PaymentConnectorStub, TestConfigService
from agent_orchestrator.testing.fixtures import (
    DEMO_BAD,
    DEMO_GOOD,
    DEMO_PROPOSAL,
    DEMO_SEED,
    demo_worker_script,
)

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")
ORACLE_OK = Oracle(
    {
        "hidden/test_oracle.py": "from parse_kv import parse_kv\n\n\ndef test_pairs():\n    assert parse_kv('x=1;y=2') == {'x': '1', 'y': '2'}\n"
    },
    "hidden/test_oracle.py",
)
ORACLE_STRICT = Oracle(
    {
        "hidden/test_oracle.py": "from parse_kv import parse_kv\n\n\ndef test_spaces():\n    assert parse_kv(' a = 1 ') == {'a': '1'}\n"
    },
    "hidden/test_oracle.py",
)


def _spec(criteria=("pytest:tests/test_parse_kv.py",)):
    def build(tenant, key):
        return MissionSpec(
            goal="实现 parse_kv(text) -> dict 并通过 tests/test_parse_kv.py",
            success_criteria=tuple(criteria),
            tenant_id=tenant,
            idempotency_key=key,
            allowed_tools=TOOLS,
            budget=Budget(max_tokens=200_000, max_attempts=4),
            workspace_seed=DEMO_SEED,
        )

    return build


def _provider(code=DEMO_GOOD, model="agent-model", attempts=1):
    def make():
        return RoleScriptedProvider(
            {
                "planner": [proposal_step(DEMO_PROPOSAL)],
                "worker": demo_worker_script(code) * attempts,
                "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
            },
            model=model,
        )

    return make


def _libraries(root: Path):
    return sorted(root.glob("runs/*/*/*/orchestrator.db"))


# ------------------------------------------------------------------ S8-03
def test_s8_03_two_strategies_on_the_same_cases_new_missions_and_honest_comparison(tmp_path):
    plan = EvaluationPlan(
        name="ab",
        cases=(EvaluationCase("parse-kv", _spec(), _provider(), oracle=ORACLE_OK),),
        strategies=(Strategy("baseline"), Strategy("no-critic", {"ablations": ("critic",)})),
        trials=2,
        config={"test_timeout_seconds": 60},
    )
    root = Path(tmp_path) / "eval"
    report = run_plan(plan, root)
    runs = report["runs"]
    assert len(runs) == 4 and all(r["category"] == "success" for r in runs)
    assert len({r["mission_id"] for r in runs}) == 4  # every run is a new Mission
    assert {r["idempotency_key"] for r in runs} == {
        f"eval:ab:{s}:parse-kv:{t}" for s in ("baseline", "no-critic") for t in (1, 2)
    }
    for library in _libraries(root):
        connection = sqlite3.connect(f"file:{library}?mode=ro", uri=True)
        assert connection.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 1
        connection.close()
    assert (
        len({json.dumps(r["budget"], sort_keys=True) for r in runs}) == 1
    )  # the same budget bound
    baseline, ablated = report["summary"]["baseline"], report["summary"]["no-critic"]
    assert baseline["samples"] == ablated["samples"] == 2 and baseline["successes"] == 2
    assert (
        baseline["wilson95"] and baseline["tokens"]["min"] > 0 and baseline["cost_micros"] is None
    )
    assert ablated["ablated_policy_passes"] == 2 and baseline["ablated_policy_passes"] == 0
    assert baseline["verification_misjudgment"] == {"checked": 2, "misjudged": 0, "rate": 0.0}
    [comparison] = report["comparisons"]
    assert comparison["verdict"].startswith("不适用（fixture）")
    assert any(d["key"] == "config.ablations" for d in comparison["policy_differences"])
    assert comparison["policy_differences_case"] == "parse-kv"
    assert [row["case"] for row in comparison["per_case"]] == ["parse-kv"]
    effects = {s["name"]: s["ablation_effects"] for s in report["strategies"]}
    assert effects["baseline"] == [] and "needs_human" in effects["no-critic"][0]
    markdown = (root / "evaluation.md").read_text(encoding="utf-8")
    assert "机制验证（fixture）" in markdown and "消融政策下的 PASS" in markdown
    assert "消融连带影响" in markdown
    assert json.loads((root / "evaluation.json").read_text(encoding="utf-8"))["plan"] == "ab"
    with pytest.raises(ValueError, match="not empty"):
        run_plan(plan, root)  # never over an earlier evaluation


def test_a_hidden_oracle_counts_what_verification_let_through(tmp_path):
    plan = EvaluationPlan(
        name="oracle",
        cases=(EvaluationCase("parse-kv-strict", _spec(), _provider(), oracle=ORACLE_STRICT),),
        strategies=(Strategy("baseline"),),
        config={"test_timeout_seconds": 60},
    )
    report = run_plan(plan, Path(tmp_path) / "eval")
    assert report["summary"]["baseline"]["verification_misjudgment"] == {
        "checked": 1,
        "misjudged": 1,
        "rate": 1.0,
    }


def test_a_failure_and_a_broken_harness_are_told_apart(tmp_path):
    failing = EvaluationCase("parse-kv-bad", _spec(), _provider(code=DEMO_BAD, attempts=2))
    broken = EvaluationCase("no-script", _spec(), lambda: RoleScriptedProvider({}))
    plan = EvaluationPlan(
        name="split",
        cases=(failing, broken),
        strategies=(Strategy("baseline"),),
        timeout_seconds=8,
        config={"test_timeout_seconds": 60},
    )
    report = run_plan(plan, Path(tmp_path) / "eval")
    categories = {r["case"]: r["category"] for r in report["runs"]}
    assert categories == {"parse-kv-bad": "failure", "no-script": "harness_error"}
    summary = report["summary"]["baseline"]
    assert summary["samples"] == 1 and summary["harness_errors"] == 1 and summary["successes"] == 0
    [sample] = summary["failure_samples"]
    assert sample["failure"]["failing_layer"] == "code_test" and summary["failure_reasons"] == {
        "max_attempts_reached": 1
    }
    broken_run = next(r for r in report["runs"] if r["category"] == "harness_error")
    assert broken_run["idempotency_key"] == "eval:split:baseline:no-script:1"
    assert broken_run["mission_id"].startswith("mission-")  # review P2-11: replayable by id


@pytest.mark.parametrize(
    "overrides",
    [
        {"deployment_policy": None},
        {"global_budget": None},
        {"hard_cap_micros": 1},
        {"knowledge_sharing": False},
        {"test_timeout_seconds": 1},
    ],
)
def test_s8_04_a_strategy_may_not_touch_budgets_policy_or_safety(tmp_path, overrides):
    plan = EvaluationPlan(
        name="x",
        cases=(EvaluationCase("c", _spec(), _provider()),),
        strategies=(Strategy("s", overrides),),
    )
    with pytest.raises(ValueError, match="may not change"):
        plan.validate()


def test_s8_04_no_critic_with_free_text_criteria_and_real_connectors_are_refused(tmp_path):
    free_text = EvaluationCase(
        "free", _spec(("pytest:tests/test_parse_kv.py", "实现应处理空字符串")), _provider()
    )
    with pytest.raises(ValueError, match="free-text"):
        EvaluationPlan(
            name="x", cases=(free_text,), strategies=(Strategy("n", {"ablations": ("critic",)}),)
        ).validate()
    paying = EvaluationCase(
        "pay",
        _spec(("file:CHANGE.md", "action:payment.pay:acct-1")),
        _provider(),
        connectors=lambda root: {"payment": PaymentConnectorStub()},
    )
    with pytest.raises(ValueError, match="test service"):
        EvaluationPlan(name="x", cases=(paying,), strategies=(Strategy("s"),)).validate()
    with pytest.raises(ValueError, match="plan may not set"):
        EvaluationPlan(
            name="x",
            cases=(free_text,),
            strategies=(Strategy("s"),),
            config={"deployment_policy": None},
        ).validate()


def test_the_statistics_are_the_textbook_ones():
    assert round(fisher_exact(3, 0, 1, 2), 4) == 0.4  # 3/3 vs 1/3 is no evidence of a difference
    assert fisher_exact(10, 0, 0, 10) < 0.001
    assert fisher_exact(0, 0, 0, 0) == 1.0
    low, high = wilson(2, 2)
    assert 0.3 < low < 0.4 and high == 1.0 and wilson(0, 0) is None


# ------------------------------------------------------------------ S8-06
def test_s8_06_an_old_task_under_a_new_model_is_a_new_evaluation(tmp_path, capsys):
    old = Path(tmp_path) / "old"
    assert (
        main(
            [
                "demo",
                "--scenario",
                "single-task",
                "--provider",
                "fixtures",
                "--evidence-dir",
                str(old),
                "--idempotency-key",
                "old-1",
            ]
        )
        == 0
    )
    capsys.readouterr()
    old_report = json.loads((old / "test-report.json").read_text(encoding="utf-8"))
    before = library_digest(old / "orchestrator.db")

    def new_model():
        return RoleScriptedProvider(
            {
                "planner": [proposal_step(DEMO_PROPOSAL)],
                "worker": demo_worker_script(DEMO_BAD) + demo_worker_script(DEMO_GOOD),
                "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
            },
            model="agent-model-b",
        )

    case = case_from_evidence(old, name="old-parse-kv", provider=new_model)
    assert (
        case.derived_from["mission_id"] == old_report["mission_id"]
        and case.derived_from["idempotency_key"] == "old-1"
    )
    plan = EvaluationPlan(
        name="rerun",
        cases=(case,),
        strategies=(Strategy("model-b", {"model": "agent-model-b"}),),
        config={"test_timeout_seconds": 60},
    )
    report = run_plan(plan, Path(tmp_path) / "eval")
    [run] = report["runs"]
    assert run["category"] == "success" and run["mission_id"] != old_report["mission_id"]
    assert run["derived_from"]["mission_id"] == old_report["mission_id"] and run["tokens"] > 0
    assert library_digest(old / "orchestrator.db") == before  # the old facts are untouched
    assert json.loads((old / "test-report.json").read_text(encoding="utf-8")) == old_report
    baseline = json.loads((old / "baseline.json").read_text(encoding="utf-8"))
    (old / "baseline.json").write_text(
        json.dumps({**baseline, "agent_orchestrator": "0.0.1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="own version"):  # review P2-10
        case_from_evidence(old, name="stale", provider=new_model)
    baseline["spec"]["goal"] = "改过的目标"
    (old / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="does not hash"):
        case_from_evidence(old, name="tampered", provider=new_model)


def test_time_and_token_differences_need_enough_samples_too():
    """Self-review (real evaluation run 1): with 2 samples a non-overlapping range is no
    evidence; with enough samples it is; fixtures are never a quality claim."""

    def summary(n, low, high):
        return {
            "successes": n,
            "samples": n,
            "enough_samples": n >= 3,
            "duration_seconds": {"min": low, "median": low, "max": high},
            "tokens": {"min": low, "median": low, "max": high},
        }

    real = EvaluationPlan(
        name="r",
        cases=(EvaluationCase("c", _spec(), _provider(), kind="env"),),
        strategies=(Strategy("a"), Strategy("b")),
    )
    few = _compare(real, "a", "b", summary(2, 50, 51), summary(2, 23, 27))
    assert few["duration"] == few["tokens"] == "证据不足（样本不足）" and few["verdict"].startswith(
        "证据不足"
    )
    many = _compare(real, "a", "b", summary(3, 50, 51), summary(3, 23, 27))
    assert many["duration"] == "有差异（区间不重叠）"
    fixture = EvaluationPlan(
        name="f",
        cases=(EvaluationCase("c", _spec(), _provider()),),
        strategies=(Strategy("a"), Strategy("b")),
    )
    assert (
        _compare(fixture, "a", "b", summary(3, 50, 51), summary(3, 23, 27))["duration"]
        == "不适用（fixture）"
    )


# ------------------------------------------------------------------ code review round 1
def test_review_p0_1_every_case_and_every_run_may_only_bring_the_test_service(tmp_path):
    quiet = EvaluationCase(
        "pay-quietly",
        _spec(),  # no action: criterion, a payment connector all the same
        _provider(),
        connectors=lambda root: {"payment": PaymentConnectorStub()},
    )
    with pytest.raises(ValueError, match="test service"):
        EvaluationPlan(name="x", cases=(quiet,), strategies=(Strategy("s"),)).validate()

    handed: list[Path] = []

    def switching(root):  # the test service for validate, a payment connector for the run
        handed.append(Path(root))
        services = {"test_config": TestConfigService(Path(root) / "config.json")}
        return services if len(handed) == 1 else {**services, "payment": PaymentConnectorStub()}

    sly = EvaluationCase("sly", _spec(), _provider(), connectors=switching)
    root = Path(tmp_path) / "eval"
    with pytest.raises(EvaluationRefused, match="test service"):
        run_plan(
            EvaluationPlan(
                name="y",
                cases=(sly,),
                strategies=(Strategy("s"),),
                config={"test_timeout_seconds": 60},
            ),
            root,
        )
    assert len(handed) == 2  # the run's own services were checked, not only the probe
    assert not list(root.rglob("orchestrator.db"))  # refused before any Mission existed
    assert not (root / "evaluation.json").exists()  # a refusal, not a harness_error row


def _row(strategy, case, category):
    return {
        "strategy": strategy,
        "case": case,
        "trial": 1,
        "category": category,
        "mission_id": "m",
        "run_dir": "d",
        "failure": None,
        "stop_reason": "x",
        "duration_seconds": 1.0,
        "tokens": 10,
        "cost_micros": None,
        "verification_pass_rate": None,
        "repeat_rate": None,
        "knowledge_reuse": 0,
        "recoveries": 0,
        "ablated_policy_pass": False,
        "oracle": None,
        "snapshot_hash": "h",
    }


def test_review_p1_5_the_comparison_is_paired_by_case_and_needs_even_harness_errors():
    plan = EvaluationPlan(
        name="r",
        cases=(
            EvaluationCase("x", _spec(), _provider(), kind="env"),
            EvaluationCase("y", _spec(), _provider(), kind="env"),
        ),
        strategies=(Strategy("a"), Strategy("b")),
    )
    runs = (
        [_row("a", "x", "success")] * 10
        + [_row("b", "x", "failure")] * 10
        + [_row("a", "y", "failure")] * 2
        + [_row("b", "y", "success")] * 2
    )

    def summaries(rows):
        return (
            _summary([r for r in rows if r["strategy"] == "a"], 3),
            _summary([r for r in rows if r["strategy"] == "b"], 3),
        )

    a, b = summaries(runs)
    assert _compare(plan, "a", "b", a, b)["verdict"].startswith(
        "成功率有差异"
    )  # pooled 10/12 vs 2/12
    paired = _compare(plan, "a", "b", a, b, runs)
    assert paired["verdict"] == "证据不足：各 case 的成功率方向不一致（合并比较可能是假象）"
    assert [(r["case"], r["higher"]) for r in paired["per_case"]] == [("x", "a"), ("y", "b")]
    same_way = [r if r["case"] == "x" else {**r, "category": "success"} for r in runs]
    a2, b2 = summaries(same_way)
    assert _compare(plan, "a", "b", a2, b2, same_way)["verdict"].startswith("成功率有差异")
    broken = [*runs, {"strategy": "b", "case": "x", "trial": 99, "category": "harness_error"}]
    a3, b3 = summaries(broken)
    uneven = _compare(plan, "a", "b", a3, b3, broken)
    assert uneven["verdict"].startswith("证据不足：两策略的脚手架错误数不同")
    assert uneven["duration"] == uneven["tokens"] == "证据不足（脚手架错误数不同）"


def test_review_p2_1_a_plan_that_cannot_build_its_configuration_is_refused_up_front():
    case = EvaluationCase("c", _spec(), _provider())
    with pytest.raises(ValueError, match="not a string"):
        EvaluationPlan(
            name="x", cases=(case,), strategies=(Strategy("s", {"ablations": "critic"}),)
        ).validate()
    with pytest.raises(ValueError, match="valid configuration"):
        EvaluationPlan(
            name="x", cases=(case,), strategies=(Strategy("s", {"ablations": ("allocator",)}),)
        ).validate()
    with pytest.raises(ValueError, match="global_budget"):
        EvaluationPlan(
            name="x",
            cases=(case,),
            strategies=(Strategy("s"),),
            config={"global_budget": {"max_tokens": 1}},
        ).validate()
