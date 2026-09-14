# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure receipt aggregation tests; these create no provider or network traffic."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256

import pytest

from agent_orchestrator.evaluation.experiment import (
    ARMS,
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
)
from agent_orchestrator.evaluation.matrix_analysis import analyze_frozen_matrix


def frozen(*, tasks=("alpha", "beta"), repetitions=1):
    manifest = ExperimentManifest(
        "matrix",
        "provider",
        "model",
        ExperimentBudget(100, 100, 200, 3, 10),
        tasks,
        repetitions,
        7,
        1,
        tuple(ArmSpec(arm, f"{arm}-v1") for arm in ARMS),
    )
    rows = []
    for index, run in enumerate(manifest.runs()):
        valid = (index + (run.arm == "S")) % 2 == 0
        runtime = valid or run.arm != "S"
        official = valid or run.arm != "R"
        rows.append(
            {
                "run": {
                    "run_id": run.run_id,
                    "arm": run.arm,
                    "task_id": run.task_id,
                    "repetition": run.repetition,
                    "seed": run.seed,
                },
                "status": "success" if valid else "failure",
                "elapsed_seconds": index + 1,
                "counters": {
                    "provider": "provider",
                    "model": "model",
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "calls": 1,
                    "peak_physical_slots": 1,
                },
                "matrix": {
                    "runtime_success": runtime,
                    "official_success": official,
                    "valid_success": valid,
                    "false_completion": False,
                    "usage": {
                        "unknown_usage_calls": 0,
                        "actual_tokens_complete": True,
                        "known_counters": {
                            "provider": "provider",
                            "model": "model",
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "total_tokens": 12,
                            "calls": 1,
                            "peak_physical_slots": 1,
                        },
                    },
                    "provider_calls": [{"input_tokens": 10, "cache_tokens": 3}],
                },
            }
        )
    receipt = {
        "schema_version": 1,
        "manifest": manifest.to_dict(),
        "manifest_sha256": manifest.fingerprint,
        "runs": rows,
    }
    identity = {
        "manifest_sha256": manifest.fingerprint,
        "source_fingerprint": "source-v1",
        "profile_fingerprint": "profile-v1",
        "config_fingerprint": "config-v1",
    }
    receipt["matrix_identity"] = deepcopy(identity)
    return receipt, identity


def test_small_matrix_is_exact_task_paired_descriptive_report():
    receipt, identity = frozen()
    report = analyze_frozen_matrix(receipt, identity, task_families={"alpha": "a", "beta": "b"})
    assert report["design"] == {
        "task_count": 2,
        "repetitions": 1,
        "arms": list(ARMS),
        "planned_runs": 8,
        "analysis_unit": "task",
    }
    assert report["arms"]["S"]["attempted"] == 2
    assert report["arms"]["S"]["token_lower_bounds"]["total_tokens"] == {
        "known": 2,
        "sum": 24.0,
        "mean": 12.0,
    }
    assert report["arms"]["S"]["cached_tokens"] == {"known": 2, "sum": 6.0, "mean": 3.0}
    assert report["arms"]["S"]["uncached_input_tokens"] == {"known": 2, "sum": 14.0, "mean": 7.0}
    assert report["paired"]["confirmed"] is True
    assert report["paired"]["utility_confirmed"] is True
    assert report["paired"]["cost_pairing"] == {"confirmed": True, "reason": None}
    assert report["paired"]["comparisons"]["S-R"]["task_count"] == 2
    assert report["paired"]["comparisons"]["S-R"]["mean_task_paired_delta"] == 0.0


def test_12_task_96_run_shape_is_aggregated_at_12_tasks():
    receipt, identity = frozen(
        tasks=tuple(f"task-{index:02d}" for index in range(12)), repetitions=2
    )
    report = analyze_frozen_matrix(receipt, identity)
    assert report["design"]["planned_runs"] == 96
    assert report["design"]["task_count"] == 12
    assert report["paired"]["comparisons"]["S-R"]["task_count"] == 12


def test_official_success_with_runtime_failure_is_not_valid_or_false_completion():
    receipt, identity = frozen(tasks=("only",))
    row = receipt["runs"][0]
    row["matrix"].update(
        runtime_success=False, official_success=True, valid_success=False, false_completion=False
    )
    report = analyze_frozen_matrix(receipt, identity)
    assert report["arms"][row["run"]["arm"]]["official_success"] == 1
    assert report["arms"][row["run"]["arm"]]["valid_success"] == 0
    assert report["arms"][row["run"]["arm"]]["false_completion"] == 0


def test_stopped_unknown_and_pending_are_explicit_and_suppress_pairs():
    receipt, identity = frozen(tasks=("only",))
    receipt["runs"][0].update(status="interrupted")
    receipt["runs"][0].pop("matrix")
    receipt["runs"][1].update(status="pending")
    receipt["runs"][1].pop("matrix")
    report = analyze_frozen_matrix(receipt, identity)
    assert report["arms"]["S"]["unknown"] == 1
    assert report["arms"]["R"]["pending"] == 1
    assert report["paired"] == {
        "confirmed": False,
        "utility_confirmed": False,
        "incomplete_units": [
            {
                "task_id": "only",
                "repetition": 0,
                "missing_arms": [],
                "nonterminal_arms": ["R"],
                "unscored_arms": ["S", "R"],
            }
        ],
        "reason": "all arms must be terminal and scored for every task/repetition",
        "comparisons": {},
        "cost_pairing": {
            "confirmed": False,
            "reason": (
                "utility pairing may be complete, but every arm needs complete actual usage "
                "and known counters before a paired cost comparison is available"
            ),
        },
    }


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda receipt, identity: identity.update(source_fingerprint=""), "source_fingerprint"),
        (
            lambda receipt, identity: receipt["runs"][0]["counters"].update(model="other"),
            "mixed provider/model",
        ),
        (
            lambda receipt, identity: receipt["runs"].__setitem__(1, deepcopy(receipt["runs"][0])),
            "identity",
        ),
    ],
)
def test_wrong_source_or_duplicate_unit_is_rejected(mutation, message):
    receipt, identity = frozen(tasks=("only",))
    mutation(receipt, identity)
    with pytest.raises(ValueError, match=message):
        analyze_frozen_matrix(receipt, identity)


def test_terminal_unscored_and_unknown_usage_are_not_zero_cost_success():
    receipt, identity = frozen(tasks=("only",))
    row = receipt["runs"][0]
    row["matrix"]["valid_success"] = None
    row["matrix"]["usage"].update(unknown_usage_calls=1, actual_tokens_complete=False)
    report = analyze_frozen_matrix(receipt, identity)
    arm = report["arms"][row["run"]["arm"]]
    assert arm["unknown_usage_calls"] == 1
    assert arm["usage_complete"] is False
    assert report["paired"]["confirmed"] is False


def test_complete_utility_pairing_does_not_claim_cost_pairing_when_usage_is_unknown():
    receipt, identity = frozen(tasks=("only",))
    row = receipt["runs"][0]
    row["matrix"]["usage"].update(unknown_usage_calls=1, actual_tokens_complete=False)
    report = analyze_frozen_matrix(receipt, identity)
    assert report["paired"]["confirmed"] is True
    assert report["paired"]["utility_confirmed"] is True
    assert report["paired"]["comparisons"]
    assert report["paired"]["cost_pairing"]["confirmed"] is False
    assert "complete actual usage" in report["paired"]["cost_pairing"]["reason"]


def test_complete_utility_pairing_requires_complete_counters_for_cost_pairing():
    receipt, identity = frozen(tasks=("only",))
    receipt["runs"][0]["matrix"]["usage"]["known_counters"].pop("total_tokens")
    report = analyze_frozen_matrix(receipt, identity)
    assert report["paired"]["utility_confirmed"] is True
    assert report["paired"]["cost_pairing"]["confirmed"] is False


@pytest.mark.parametrize("unknown", [True, 0.5])
def test_unknown_usage_count_must_be_a_nonnegative_integer(unknown):
    receipt, identity = frozen(tasks=("only",))
    receipt["runs"][0]["matrix"]["usage"]["unknown_usage_calls"] = unknown
    with pytest.raises(ValueError, match="unknown_usage_calls"):
        analyze_frozen_matrix(receipt, identity)


@pytest.mark.parametrize(
    ("unknown", "complete"),
    [(0, False), (1, True)],
)
def test_usage_completeness_must_match_unknown_usage_count(unknown, complete):
    receipt, identity = frozen(tasks=("only",))
    receipt["runs"][0]["matrix"]["usage"].update(
        unknown_usage_calls=unknown, actual_tokens_complete=complete
    )
    with pytest.raises(ValueError, match="completeness"):
        analyze_frozen_matrix(receipt, identity)


def test_report_identity_hashes_the_entire_canonical_matrix_identity():
    receipt, identity = frozen(tasks=("only",))
    identity.update(
        environment_fingerprint="environment-v1",
        environment_url_sha256="a" * 64,
        run_window={"schedule_id": "off-peak-v1"},
    )
    receipt["matrix_identity"] = deepcopy(identity)
    report = analyze_frozen_matrix(receipt, identity)
    canonical = json.dumps(
        identity, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )
    assert report["identity"]["matrix_identity_sha256"] == sha256(
        canonical.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("cached", [float("nan"), float("inf"), 11])
def test_invalid_cache_cannot_produce_negative_or_nonfinite_uncached(cached):
    receipt, identity = frozen(tasks=("only",))
    row = receipt["runs"][0]
    row["matrix"]["provider_calls"][0]["cache_tokens"] = cached
    arm = analyze_frozen_matrix(receipt, identity)["arms"][row["run"]["arm"]]
    assert arm["cached_tokens"] is None
    assert arm["uncached_input_tokens"] is None


def test_another_source_identity_cannot_be_attached_to_existing_receipt():
    receipt, identity = frozen(tasks=("only",))
    identity["source_fingerprint"] = "another-valid-source"
    with pytest.raises(ValueError, match="source/profile/config identity"):
        analyze_frozen_matrix(receipt, identity)


def test_nested_counter_model_mismatch_is_rejected():
    receipt, identity = frozen(tasks=("only",))
    receipt["runs"][0]["matrix"]["usage"]["known_counters"]["model"] = "other"
    with pytest.raises(ValueError, match="mixed provider/model"):
        analyze_frozen_matrix(receipt, identity)
