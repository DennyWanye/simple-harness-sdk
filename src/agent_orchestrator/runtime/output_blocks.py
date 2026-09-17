# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Parse the single tagged JSON block an Agent must return (§13: no free prose).

``extract_block(text, tag)`` returns the parsed JSON object of the one
``<tag>…</tag>`` block or raises ``BlockError`` with a reason that is fed back to
the Agent on the repair Attempt (``envelope_invalid``).  Zero or several blocks,
non-object JSON and trailing garbage inside the block are all rejected; text
*outside* the block is tolerated but recorded (models add prose).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class BlockError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def extract_block(text: str, tag: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise BlockError("empty_output")
    pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
    matches = pattern.findall(text)
    if not matches:
        raise BlockError("block_missing", f"no <{tag}> block in the output")
    if len(matches) > 1:
        raise BlockError("block_ambiguous", f"{len(matches)} <{tag}> blocks")
    body = matches[0].strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise BlockError("invalid_json", f"{error.msg} at line {error.lineno}") from error
    if not isinstance(parsed, dict):
        raise BlockError("not_an_object", type(parsed).__name__)
    return parsed


@dataclass(frozen=True, slots=True)
class PortClaim:
    """ "This file is what I produced at that output port" — said by the Worker.

    P2.3c part 2d, decision 4.  Which of an Attempt's files is the ``repository_facts``
    a downstream step declared a DATA edge for is a **local key**: only the agent that
    wrote the file knows it, and TG design §3.2 draws the line exactly there — the
    model supplies local keys, business goals and parameters, while Mission identity,
    accounts, scope and execution generation are bound by the system.  Everything else
    an :class:`~..artifacts.input_bindings.AcceptedOutput` carries (acceptance id,
    content hash, schema ref, producer result, support revision, occurrence) is
    system-bound and is never read from the model.

    So the claim has exactly two fields, and the parser refuses a third.  Before this,
    the port was *inferred* from the artifact path by substring match — which paired
    an ``artifacts/…`` file with a port named ``facts`` — or by "one port and one file,
    so they must go together".  Both are the guess TG design §10.2 forbids.
    """

    port_key: str
    path: str


def parse_port_claims(
    value: object,
    *,
    declared_ports: Sequence[str],
    attempt_paths: Sequence[str],
    single_valued: Sequence[str] = (),
    where: str = "result_envelope.outputs",
) -> tuple[PortClaim, ...]:
    """Read the ``outputs`` map of a hierarchical Worker's envelope, or refuse it.

    Four structural refusals, each of which is a defect the model can repair on the
    same Attempt (§18.5 C8 bounded repair), never a silent drop:

    1. a port the plan never declared for this occurrence — the model would be
       claiming the plan says something it does not say;
    2. a path this Attempt did not produce — a claim about a file nobody saw written;
    3. the same single-valued port claimed twice — §24.1 decision 3 gives a
       single-valued port exactly one binding;
    4. any key other than a declared port — including a ``schema`` or ``acceptance_id``
       the model tried to bind itself.
    """

    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise BlockError(
            "outputs_not_an_object",
            f"{where} must be an object, not {type(value).__name__}",
        )
    known = set(declared_ports)
    produced = set(attempt_paths)
    single = set(single_valued) or known
    claims: list[PortClaim] = []
    seen: set[str] = set()
    for key, raw in value.items():
        port = str(key)
        if port not in known:
            raise BlockError(
                "output_port_not_declared",
                f"{where} names port {port!r}, which this task does not declare; "
                f"declared: {sorted(known)}",
            )
        if port in seen and port in single:
            raise BlockError(
                "output_port_claimed_twice",
                f"{where} binds the single-valued port {port!r} more than once",
            )
        seen.add(port)
        if not isinstance(raw, str) or not raw.strip():
            raise BlockError(
                "output_path_not_a_string",
                f"{where}[{port!r}] must be the path of one file you wrote",
            )
        path = raw.strip()
        if path not in produced:
            raise BlockError(
                "output_path_not_produced",
                f"{where}[{port!r}] names {path!r}, which this attempt did not write; "
                f"produced: {sorted(produced)}",
            )
        claims.append(PortClaim(port_key=port, path=path))
    return tuple(claims)


def outside_text(text: str, tag: str) -> str:
    return re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL).strip()


#: What to say back to the Agent for each way a block can be unreadable (§18.5 C8).
#: The repair is *bounded*: the same Attempt is told precisely what was wrong and
#: asked again, and a malformed block never becomes a second request that quietly
#: launders the failure into a fresh identity.
REPAIR_HINTS: dict[str, str] = {
    "empty_output": "你没有输出任何内容。只输出一个 <{tag}> 块。",
    "block_missing": "没有找到 <{tag}> 块。只输出一个 <{tag}>…</{tag}> 块，块内是 JSON 对象。",
    "block_ambiguous": "输出里有多个 <{tag}> 块。只保留一个。",
    "invalid_json": "<{tag}> 块内不是合法 JSON（{detail}）。重新输出完整的 JSON 对象。",
    "not_an_object": "<{tag}> 块内必须是 JSON 对象，不是 {detail}。",
    "outputs_not_an_object": "outputs 必须是对象：{{端口名: 文件路径}}（{detail}）。",
    "output_port_not_declared": (
        "outputs 里的端口名只能抄输入 declared_output_ports 里给你的那些，不能自己造（{detail}）。"
    ),
    "output_port_claimed_twice": "一个单值输出端口只能对应一个文件（{detail}）。",
    "output_path_not_a_string": "outputs 的每个值必须是一个文件路径字符串（{detail}）。",
    "output_path_not_produced": "outputs 里的路径必须是你本次真实写过的文件（{detail}）。",
    # P2.3g: the Planner answered with the *other* role's block.  Not ``block_missing``
    # — the model did write a block, the wrong one — and the hint says whose job the
    # method is and what to write instead.
    "proposal_wrong_block": (
        "你是 Planner，不提方法：不要输出 <method_proposal> 块。没有可用方法就输出一个 "
        "operations 为空、rationale 以 \"no_applicable_method: \" 开头的 <{tag}>…</{tag}> 块"
        "（{detail}）。"
    ),
}


def repair_hint(error: BlockError, tag: str) -> str:
    """The one instruction the repair Attempt is given for ``error``.

    A single sentence naming the tag and the defect, because the model is being
    asked to fix *this* output — not to be told the whole contract again.
    """

    template = REPAIR_HINTS.get(
        error.reason, "<{tag}> 块无法解析（{detail}）。按契约重新输出该块。"
    )
    return template.format(tag=tag, detail=error.detail or error.reason)


__all__ = (
    "REPAIR_HINTS",
    "BlockError",
    "PortClaim",
    "extract_block",
    "outside_text",
    "parse_port_claims",
    "repair_hint",
)
