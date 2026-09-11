# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Independent Critic (§14.1 layer 3, §9.2 role Critic) as a BaseAgent call.

The Critic is its own Agent (``creation_key = <attempt_id>:critic:<n>``, D22),
sees only the verification copy through read-only tools plus the deterministic
test output, never the Worker's own explanation (§10.2), and answers with one
``<critic_verdict>`` block.  Its call is dispatched through the same intent
machinery as a Worker so it is budgeted, replayable and settled (ORCH §12.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts import ContractError
from ..runtime.output_blocks import BlockError, extract_block
from ..runtime.role_templates import CRITIC_VERDICT_TAG


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    verdict: str
    findings: tuple[Mapping[str, Any], ...]
    mission_criteria: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]
    # step 7 (D7-8'): the Critic cannot reliably judge the success condition (original §22);
    # only meaningful on a PASS — a blocker is a FAIL whatever else the Critic says
    needs_human: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [dict(item) for item in self.findings],
            "mission_criteria": [dict(item) for item in self.mission_criteria],
            "needs_human": self.needs_human,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CriticVerdict:
        """A verdict recorded as a verification layer, read back on resume (D7-8')."""

        return cls(
            verdict=str(value.get("verdict", "FAIL")),
            findings=tuple(dict(item) for item in value.get("findings", [])),
            mission_criteria=tuple(dict(item) for item in value.get("mission_criteria", [])),
            raw=dict(value),
            needs_human=bool(value.get("needs_human", False)),
        )


def parse_critic_verdict(text: str, *, expected_criteria: Sequence[str]) -> CriticVerdict:
    """Strict parse; a malformed verdict is a verification ERROR, never a PASS."""

    try:
        raw = extract_block(text, CRITIC_VERDICT_TAG)
    except BlockError as error:
        raise ContractError(f"critic verdict unreadable: {error}") from error
    verdict = raw.get("verdict")
    if verdict not in {"PASS", "FAIL"}:
        raise ContractError("critic verdict must be PASS or FAIL")
    findings = raw.get("findings", [])
    if not isinstance(findings, list) or any(not isinstance(item, Mapping) for item in findings):
        raise ContractError("critic findings must be a list of objects")
    blockers = [item for item in findings if item.get("severity") == "blocker"]
    if (verdict == "FAIL") != bool(blockers):
        raise ContractError("critic verdict must be FAIL iff a blocker finding exists")
    criteria = raw.get("mission_criteria", [])
    if not isinstance(criteria, list) or any(not isinstance(item, Mapping) for item in criteria):
        raise ContractError("critic mission_criteria must be a list of objects")
    seen = [str(item.get("criterion")) for item in criteria]
    if seen != list(expected_criteria):
        raise ContractError("critic mission_criteria must cover the Mission criteria in order")
    for item in criteria:
        if not isinstance(item.get("met"), bool):
            raise ContractError("critic mission_criteria[].met must be boolean")
    needs_human = raw.get("needs_human", False)
    if not isinstance(needs_human, bool):
        raise ContractError("critic needs_human must be boolean")
    return CriticVerdict(
        verdict=str(verdict),
        findings=tuple(dict(item) for item in findings),
        mission_criteria=tuple(dict(item) for item in criteria),
        raw=raw,
        needs_human=needs_human and verdict == "PASS",  # a blocker always wins
    )


__all__ = ("CriticVerdict", "parse_critic_verdict")
