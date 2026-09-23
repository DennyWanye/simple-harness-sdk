# ruff: noqa: E501
from __future__ import annotations

import pytest

from agent_orchestrator.planning.htn.planner_package import hierarchical_planner_package
from agent_orchestrator.planning.htn.planner_package_v1 import (
    MAX_PACKAGE_BYTES,
    PlannerPackageAssemblerV1,
    PlannerPackageCodec,
    PlannerPackageCodecError,
    PlannerPackageV1,
)


def _legacy(*, facts=(), accepted=(), failures=(), refs=()):
    return {
        "mission": {"mission_id": "m-1", "goal": "ship", "budget": {"max_tokens": 1000}},
        "plan": {
            "plan_revision": 3,
            "open_compound_goals": [{
                "occurrence_id": "occ-1", "goal_id": "task-1", "obligation_id": "obl-1",
                "goal_signature_id": "sig.ship", "statement": "ship artifact", "typed_parameters": {"x": "a"},
                "requirement_refs": ["req-1"], "contract_revision": 2, "requiredness": "REQUIRED",
            }],
            "committed_primitives": [], "required_obligations": ["obl-1"], "root_occurrences": ["occ-1"],
        },
        "method_library": [{
            "goal_signature_id": "sig.ship", "refine_method_ref": {"id": "m.ship", "version": 1, "content_hash": "h"},
            "registry_status": "PROMOTED", "required_capabilities": ["artifact.write"], "steps": [],
        }],
        "facts": list(facts), "accepted_results": list(accepted), "planning_rejected": list(failures),
        "rejected_refinements": [], "operators": {"available_capabilities": ["artifact.write"], "unavailable_capabilities": []},
        "visible_refs": list(refs),
    }


def test_nine_views_round_trip_and_replay_hash_is_stable():
    package = PlannerPackageAssemblerV1().assemble(_legacy())
    assert set(package.views_json()) == {"goals", "obligations", "plans", "methods", "facts", "accepted_results", "failures", "capabilities", "planning_budgets"}
    restored = PlannerPackageV1.from_json(package.to_json())
    assert restored.canonical_bytes() == package.canonical_bytes()
    assert restored.content_hash() == package.content_hash()
    assert package.goals[0].subject_key == "occurrence:occ-1"
    assert PlannerPackageCodec.decode(PlannerPackageCodec.encode(package)).content_hash() == package.content_hash()


def test_limits_are_deterministic_and_report_omitted_counts():
    raw = _legacy(facts=[{"proposition_key": str(i), "polarity": True, "coverage": "full", "observer_id": "o", "observed_at_ms": i, "read_set_entry": {"id": str(i)}} for i in range(40)])
    package = PlannerPackageAssemblerV1().assemble(raw, visible_refs=[{"id": str(i)} for i in range(128)])
    assert package.truncated is True
    assert package.omitted_counts == {"facts": 16}
    assert len(package.facts) == 24
    assert len(package.visible_refs) == 128
    with pytest.raises(PlannerPackageCodecError, match="required visible references exceed 128"):
        PlannerPackageAssemblerV1().assemble(
            raw, visible_refs=[{"id": str(i)} for i in range(129)]
        )


def test_codec_rejects_unknown_fields_and_oversized_mandatory_package():
    payload = PlannerPackageAssemblerV1().assemble(_legacy()).to_json()
    payload["unexpected"] = True
    with pytest.raises(PlannerPackageCodecError):
        PlannerPackageV1.from_json(payload)


def test_size_pressure_drops_optional_evidence_with_a_receipt():
    huge = [{"proposition_key": str(i), "polarity": True, "coverage": "x" * 6000, "observer_id": "o", "observed_at_ms": i, "read_set_entry": {"id": str(i)}} for i in range(24)]
    package = PlannerPackageAssemblerV1().assemble(_legacy(facts=huge))
    assert len(package.canonical_bytes()) <= MAX_PACKAGE_BYTES
    assert package.truncated is True
    assert package.omitted_counts["facts"] > 0
    payload = PlannerPackageAssemblerV1().assemble(_legacy()).to_json()
    payload["views"]["goals"][0]["statement"] = "x" * (MAX_PACKAGE_BYTES * 2)
    with pytest.raises(PlannerPackageCodecError):
        PlannerPackageV1.from_json(payload)


def test_legacy_collector_bytes_are_unchanged_by_h2_import():
    # H2 is an additive envelope; callers still get the existing legacy shape.
    assert hierarchical_planner_package.__module__.endswith("planner_package")
    assert "planner-package-hierarchical-v9" not in hierarchical_planner_package.__doc__


def test_accepted_and_failure_caps_preserve_exact_omission_counts():
    package = PlannerPackageAssemblerV1().assemble(_legacy(
        accepted=[{"producer_occurrence": f"occ-{i}"} for i in range(25)],
        failures=[{"reason": f"failure-{i}"} for i in range(17)],
    ))
    assert len(package.accepted_results) == 24
    assert len(package.failures) == 16
    assert package.omitted_counts == {"accepted_results": 1, "failures": 1}


def test_method_cap_is_per_signature_and_reports_all_omissions():
    raw = _legacy()
    base = raw["method_library"][0]
    raw["method_library"] = [
        {**base, "goal_signature_id": signature,
         "refine_method_ref": {"id": f"{signature}-{i}", "version": 1, "content_hash": "h"}}
        for signature in ("ship", "review") for i in range(13)
    ]
    package = PlannerPackageAssemblerV1().assemble(raw)
    assert len(package.methods) == 24
    for signature in ("ship", "review"):
        assert sum(item.goal_signature["id"] == signature for item in package.methods) == 12
    assert package.omitted_counts == {"methods": 2}


def test_omitted_counts_are_integers_without_coercion():
    for invalid in (True, 0.5, "1", -1):
        payload = PlannerPackageAssemblerV1().assemble(_legacy()).to_json()
        payload["omitted_counts"] = {"facts": invalid}
        payload["truncated"] = True
        with pytest.raises(PlannerPackageCodecError, match="non-negative integers"):
            PlannerPackageV1.from_json(payload)
