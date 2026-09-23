# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Section E skill instruction blocks (ARP-EXEC-1.1.1 §9.8).

Every ``skill.load`` of this Session contributes its instruction files; the same file
(skill revision + path + sha256) appears once however often it was loaded. The block
is bound to the load receipt and the file artefact, so a frozen manifest replays from
its own section list and never from the current load set.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from simple_harness.contracts import Message, MessageRole

from .. import catalogue as cat
from .. import store
from ..errors import ArpError
from ..pins import Pin

_SKILL_TAG = re.compile(r"<(/?)skill_instructions", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SkillBlock:
    skill_ref: Pin
    path: str
    text: str
    tokens: int
    receipt_ref: Pin
    artifact_ref: Pin

    @property
    def block_id(self) -> str:
        return f"skill:{self.skill_ref.id}@{self.skill_ref.revision}:{self.path}"


def skill_message(block: SkillBlock) -> Message:
    """Loaded instructions are data inside a named frame, not a new control prompt."""

    body = _SKILL_TAG.sub(r"&lt;\1skill_instructions", block.text)
    return Message(
        MessageRole.USER,
        f'<skill_instructions skill="{block.skill_ref.id}@{block.skill_ref.revision}" path="{block.path}" sha256="{block.artifact_ref.content_hash}">\n{body}\n</skill_instructions>',
        metadata={"derived": True, "skill_ref": block.skill_ref.to_json(), "file_sha256": block.artifact_ref.content_hash},
    )


def _block(arp: Any, skill_ref: Pin, path: str, sha256: str, receipt_ref: Pin, count: Callable[[str], int]) -> SkillBlock:
    revision = cat.read_revision(arp.catalogue.connection, arp.catalogue.namespace_id, "SKILL", skill_ref.id, skill_ref.revision)
    if revision is None or revision.content_hash != skill_ref.content_hash:
        raise ArpError("CATALOGUE_STALE", f"loaded skill {skill_ref.id}@{skill_ref.revision} is no longer the same definition")
    data = arp.skills.read_file(revision, path)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ArpError("SOURCE_HASH_CONFLICT", f"{path}: bytes differ from the load receipt")
    text = data.decode("utf-8", "replace")
    artifact = Pin("artifact", f"skill:{skill_ref.id}@{skill_ref.revision}:{path}", 0, sha256)
    return SkillBlock(skill_ref, path, text, count(text), receipt_ref, artifact)


def skill_blocks_for(arp: Any, session: store.SessionRow, *, count: Callable[[str], int]) -> tuple[SkillBlock, ...]:
    """Distinct instruction files of every INSTRUCTIONS use of this Session, load order."""

    if getattr(arp, "skills", None) is None:
        return ()
    connection = arp.catalogue.connection
    now_ms = arp.catalogue.clock_ms()
    blocks: dict[tuple[str, int, str, str], SkillBlock] = {}
    usable: dict[tuple[str, int], bool] = {}
    for use in store.read_skill_uses(connection, session.session_id, mode="INSTRUCTIONS"):
        receipt = store.read_original_receipt(connection, kind="skill_load", receipt_key=str(use["use_id"]))
        if receipt is None:
            continue  # an execute of an INSTRUCTIONS skill: returned inline, not loaded into E
        skill_ref = Pin.from_json(use["skill_ref"])
        if not usable.setdefault((skill_ref.id, skill_ref.revision), _still_usable(arp, skill_ref, now_ms)):
            continue  # suspended / retired / lock revoked since the load: no new exposure (§9.5, §9.7)
        receipt_pin = Pin("receipt", f"skill_load:{use['use_id']}", 0, _receipt_hash(receipt))
        for item in receipt["files"]:
            key = (skill_ref.id, skill_ref.revision, str(item["relative_path"]), str(item["sha256"]))
            if key not in blocks:
                blocks[key] = _block(arp, skill_ref, key[2], key[3], receipt_pin, count)
    return tuple(blocks.values())


def _still_usable(arp: Any, skill_ref: Pin, now_ms: int) -> bool:
    """ADMITTED, usable right now, with a complete dependency lock."""

    connection = arp.catalogue.connection
    revision = cat.read_revision(connection, arp.catalogue.namespace_id, "SKILL", skill_ref.id, skill_ref.revision)
    if revision is None:
        return False
    activation = cat.read_activation(connection, arp.catalogue.namespace_id, "SKILL", revision.entry_id, revision.revision)
    if activation is None or not arp.catalogue.usable(activation, now_ms=now_ms)[0]:
        return False
    lock = arp.skills.latest_lock(revision)
    return lock is not None and bool(lock["complete"])


def replay_skill_blocks(arp: Any, manifest: Mapping[str, Any], *, count: Callable[[str], int]) -> tuple[SkillBlock, ...]:
    """The frozen manifest's own E skill blocks (block id → skill pin from skill_refs)."""

    skills = {f"{p['id']}@{p['revision']}": Pin.from_json(p) for p in manifest.get("skill_refs", [])}
    blocks: list[SkillBlock] = []
    for section in manifest["sections"]:
        if section["section"] != "E" or not str(section["block_id"]).startswith("skill:"):
            continue
        _, _, rest = str(section["block_id"]).partition(":")
        key, _, path = rest.partition(":")
        skill_ref = skills.get(key)
        receipt = next((Pin.from_json(r) for r in section["source_refs"] if r["kind"] == "receipt"), None)
        if skill_ref is None or receipt is None:
            raise ArpError("REQUEST_HASH_MISMATCH", f"frozen skill block {section['block_id']} has no skill pin or receipt")
        blocks.append(_block(arp, skill_ref, path, str(section["view_ref"]["content_hash"]), receipt, count))
    return tuple(blocks)


def _receipt_hash(body: Mapping[str, Any]) -> str:
    from ..strict import digest

    return digest(body)


__all__ = ("SkillBlock", "replay_skill_blocks", "skill_blocks_for", "skill_message")
