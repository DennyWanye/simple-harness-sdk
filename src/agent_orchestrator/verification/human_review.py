# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Human review — the sixth verification layer (original §14.1 "必要时人工审核", §24
NEEDS_HUMAN; ORCH §9.2 human_review row "把 NEEDS_HUMAN 和 Verifier 冲突送人工"; plan D7-8').

A person is asked when the Task's policy names ``human_review`` or when the Critic cannot
reliably judge (``needs_human``, original §22 "模型无法可靠判断成功条件").  The layer never
short-circuits the others: the code tests still run first, and a person's PASS never
covers a failing layer.  While nobody has answered, the result is SUSPENDED.

A Verifier conflict also goes to a person, as arbitration (this build's two kinds, plan
D7-8'): a step-4 Conflict Task that could not settle a contradiction, and a Mission-level
judge Critic that finds a criterion unmet although every deterministic criterion is met
and the Tasks' own Critics passed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .deterministic_checks import FAIL, PASS, LayerResult

NEEDS_HUMAN = "NEEDS_HUMAN"
SUSPENDED = "SUSPENDED"


def review_request_id(result_id: str) -> str:
    return f"review-{result_id}"


def arbitration_request_id(subject: str) -> str:
    return f"arbitration-{subject}"


def human_layer(decision: Mapping[str, Any] | None) -> LayerResult:
    """The sixth layer: SUSPENDED until a person answered, then that person's verdict."""

    if decision is None:
        return LayerResult("human_review", SUSPENDED, "waiting for a person's review", {})
    passed = decision.get("verdict") == "PASS"
    note = str(decision.get("note") or "")
    return LayerResult(
        "human_review",
        PASS if passed else FAIL,
        ("a person passed it" if passed else "a person failed it") + (f": {note}" if note else ""),
        {
            "principal_id": decision.get("principal"),
            "note": note,
            "request_id": decision.get("request_id"),
        },
    )


def reusable_layers(
    rows: Sequence[Mapping[str, Any]], *, versions: Mapping[str, str], default_version: str
) -> dict[str, LayerResult]:
    """Layers recorded before the suspension that may stand on resume: a PASS (or the
    Critic's NEEDS_HUMAN) written by the verifier version in use now (review P1-6 ②).
    The result is immutable, so nothing is asked twice — no second Critic, no second run."""

    reuse: dict[str, LayerResult] = {}
    for row in rows:
        layer, status = str(row["layer"]), str(row["status"])
        detail = dict(row.get("detail") or {})
        if layer == "human_review" or status not in {PASS, NEEDS_HUMAN}:
            continue
        if detail.get("verifier_version") != versions.get(layer, default_version):
            continue
        reuse[layer] = LayerResult(layer, status, str(detail.get("summary", "")), detail)
    return reuse


def judgment_conflict(
    judgments: Sequence[Mapping[str, Any]], *, task_critic_passes: int
) -> list[str]:
    """The criteria on which Verifiers disagree (plan D7-8' kind ②): an *independent*
    judge Critic found them unmet, while every deterministic criterion is met and the
    Tasks' own Critics passed.  Empty = no conflict (the ordinary judgment stands)."""

    if task_critic_passes <= 0:
        return []
    deterministic = [j for j in judgments if j.get("judge") in {"code_test", "rule_check"}]
    if not all(bool(j.get("met")) for j in deterministic):
        return []
    return [
        str(j.get("criterion"))
        for j in judgments
        if j.get("judge") == "critic_review"
        and j.get("source") == "independent"
        and not j.get("met")
    ]


__all__ = (
    "NEEDS_HUMAN",
    "SUSPENDED",
    "arbitration_request_id",
    "human_layer",
    "judgment_conflict",
    "review_request_id",
    "reusable_layers",
)
