"""Skill bundle import (§9.1–9.5): SKILL.md-only → INSTRUCTIONS candidate with zero
authority, native skill.json checked file by file, strict frontmatter, path safety,
dependency locks (unresolved keeps QUARANTINED; diamonds and cycles refused), details
paging and hash-verified file reads."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.skills import inspect_bundle, parse_frontmatter
from simple_harness.agents.arp.strict import digest

FRONT = "---\nname: my-skill\ndescription: 一个只读的说明技能。\nlicense: MIT\nmetadata:\n  author: test\nallowed-tools: session_history_search\n---\n# 用法\n\n先读这里。\n"


def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "SKILL.md")
    return buffer.getvalue()


def _artifact(data: bytes) -> Pin:
    return Pin("artifact", "bundle:" + hashlib.sha256(data).hexdigest()[:12], 0, hashlib.sha256(data).hexdigest())


def _command(runtime, data: bytes, fmt: str = "SKILL_MD") -> dict:  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    return {"bundle_artifact_ref": _artifact(data).to_json(), "scope_ref": service.scope.pin.to_json(), "format": fmt, "expected_catalogue_revision": service.epoch()}


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_skill_md_bundle_becomes_a_quarantined_instructions_candidate(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            importer = runtime.arp.skills
            service = runtime.arp.catalogue
            data = _zip({"SKILL.md": FRONT.encode(), "scripts/run.py": b"print('never executed')\n", "notes/ref.md": "参考。".encode()})
            epoch = service.epoch()
            result = importer.import_bundle(_command(runtime, data), data, caller=trusted_caller(), command_id="install-1")
            skill = result.revision
            assert skill.entry_kind == "SKILL" and skill.entry_id == "my-skill" and result.activation.state == "QUARANTINED"
            assert skill.body["implementation"] == {"kind": "INSTRUCTIONS"} and skill.body["required_tool_refs"] == []
            roles = {f["relative_path"]: f["role"] for f in skill.body["files"]}
            assert roles == {"SKILL.md": "INSTRUCTIONS", "scripts/run.py": "ASSET", "notes/ref.md": "REFERENCE"}
            assert skill.body["capability_ref"] == runtime.arp.bootstrap.instructions_capability_ref.to_json()
            assert result.frontmatter["allowed-tools"] == ["session_history_search"]  # a suggestion, not an authority
            assert result.lock["complete"] is True and result.lock["nodes"] == [] and service.epoch() == epoch + 1
            # Bytes come back exactly, verified against the definition.
            assert importer.read_file(skill, "notes/ref.md") == "参考。".encode()
            with pytest.raises(ArpError) as refused:
                importer.read_file(skill, "missing.md")
            assert refused.value.code == "SKILL_PATH_INVALID"
            # The same bundle again: replay, same revision, no epoch step.
            again = importer.import_bundle(_command(runtime, data), data, caller=trusted_caller(), command_id="install-1")
            assert again.replayed and again.revision.revision == skill.revision and service.epoch() == epoch + 1
            # Details page: bounded file listing with an epoch-independent cursor.
            first = importer.details({"skill_ref": skill.pin.to_json(), "cursor": None, "limit": 2})
            assert len(first["files"]) == 2 and first["has_more"] and first["bundle_digest"] == result.bundle_digest
            second = importer.details({"skill_ref": skill.pin.to_json(), "cursor": first["next_cursor"], "limit": 2})
            assert [f["relative_path"] for f in first["files"] + second["files"]] == sorted(roles) and not second["has_more"]
            # Catalogue page shows it as not usable while quarantined, with file counts (no file list).
            page = service.page({"namespace_id": service.namespace_id, "kind": "SKILL", "cursor": None, "limit": 10}, access_view="MODEL")
            item = page["items"][0]
            assert item["name"] == "my-skill" and item["current_usable"] is False and item["file_count"] == 3 and "files" not in item
            # Command guards.
            with pytest.raises(ArpError) as stale:
                importer.import_bundle({**_command(runtime, data), "expected_catalogue_revision": 0}, data, caller=trusted_caller(), command_id="install-2")
            assert stale.value.code == "EXPECTED_REVISION_MISMATCH"
            with pytest.raises(ArpError) as tampered:
                importer.import_bundle(_command(runtime, data), data + b"x", caller=trusted_caller(), command_id="install-3")
            assert tampered.value.code == "SOURCE_HASH_CONFLICT"

    _run(case())


@pytest.mark.parametrize(
    "text, code",
    [
        ("name: x\ndescription: y\n", "SKILL_FRONTMATTER_INVALID"),  # no leading ---
        ("---\nname: my-skill\ndescription: y\n", "SKILL_FRONTMATTER_INVALID"),  # unclosed
        ("---\nname: my-skill\nname: other\ndescription: y\n---\n", "SKILL_FRONTMATTER_INVALID"),  # duplicate key
        ("---\nname: &a my-skill\ndescription: *a\n---\n", "SKILL_FRONTMATTER_INVALID"),  # anchor/alias
        ("---\nname: !!python/object:os.system my-skill\ndescription: y\n---\n", "SKILL_FRONTMATTER_INVALID"),  # tag
        ("---\nname: my-skill\ndescription: y\nscripts: run.py\n---\n", "SKILL_FRONTMATTER_INVALID"),  # unknown execution key
        ("---\nname: My_Skill\ndescription: y\n---\n", "SKILL_FRONTMATTER_INVALID"),  # non-portable name
        ("---\nbase: &b {a: 1}\nname: my-skill\ndescription: y\n<<: *b\n---\n", "SKILL_FRONTMATTER_INVALID"),  # merge key
        ("---\nname: my-skill\ndescription: y\nmetadata:\n  count: 3\n---\n", "SKILL_FRONTMATTER_INVALID"),  # non-string metadata
        ("---\n" + "k: v\n" * 4000 + "---\n", "ITEM_TOO_LARGE"),  # frontmatter > 16KiB
    ],
)
def test_frontmatter_rejections_are_named(text: str, code: str) -> None:
    with pytest.raises(ArpError) as refused:
        parse_frontmatter(text.encode())
    assert refused.value.code == code


def test_frontmatter_accepts_the_allowed_subset_only() -> None:
    parsed = parse_frontmatter(FRONT.encode())
    assert parsed["name"] == "my-skill" and parsed["license"] == "MIT" and parsed["metadata"] == {"author": "test"}
    # No implicit date/object types: a date-looking value stays the string it was written as.
    dated = parse_frontmatter(b"---\nname: my-skill\ndescription: y\nmetadata:\n  when: 2026-01-01\n---\n")
    assert dated["metadata"] == {"when": "2026-01-01"}


@pytest.mark.parametrize(
    "entries, symlink, code",
    [
        ({"SKILL.md": b"---\nname: a\ndescription: b\n---\n", "../escape.md": b"x"}, None, "SKILL_PATH_INVALID"),
        ({"SKILL.md": b"---\nname: a\ndescription: b\n---\n", "Docs/A.md": b"1", "docs/a.md": b"2"}, None, "SKILL_PATH_COLLISION"),
        ({"SKILL.md": b"---\nname: a\ndescription: b\n---\n", "CON.txt": b"x"}, None, "SKILL_PATH_RESERVED"),
        ({"SKILL.md": b"---\nname: a\ndescription: b\n---\n"}, "link.md", "SKILL_PATH_INVALID"),
        ({"SKILL.md": b"---\nname: a\ndescription: b\n---\n", "big.bin": b"\0" * (8 * 1024 * 1024 + 1)}, None, "ITEM_TOO_LARGE"),
        ({"SKILL.md": b"---\nname: a\ndescription: b\n---\n" + b"x" * (128 * 1024)}, None, "ITEM_TOO_LARGE"),
        ({**{f"f{i}.txt": b"x" for i in range(1025)}}, None, "ARRAY_LIMIT"),
    ],
)
def test_bundle_rejections_are_named(entries: dict, symlink: str | None, code: str) -> None:
    with pytest.raises(ArpError) as refused:
        inspect_bundle(_zip(entries, symlink=symlink))
    assert refused.value.code == code


def test_native_bundle_is_checked_file_by_file_and_locks_its_dependencies(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            importer = runtime.arp.skills
            service = runtime.arp.catalogue
            report = runtime.arp.bootstrap
            tool = cat.latest_revision(service.connection, service.namespace_id, "TOOL", "session_history_search").pin
            body_md = "# 说明\n".encode()

            def manifest(**overrides):  # type: ignore[no-untyped-def]
                base = {
                    "schema_version": 1, "skill_id": "native-one", "version": 1, "name": "native-one", "description": "原生技能。",
                    "files": [{"relative_path": "SKILL.md", "size_bytes": len(body_md), "sha256": hashlib.sha256(body_md).hexdigest(), "role": "INSTRUCTIONS"}],
                    "instructions_path": "SKILL.md",
                    "input_schema_ref": report.instructions_schema_ref.to_json(), "output_schema_ref": report.instructions_schema_ref.to_json(),
                    "capability_ref": report.instructions_capability_ref.to_json(),
                    "required_tool_refs": [tool.to_json()], "required_skill_refs": [],
                    "implementation": {"kind": "INSTRUCTIONS"},
                    "requested_permission_policy_ref": Pin("policy", "p", 0, digest("p")).to_json(),
                    "verification_policy_ref": report.verification_policy_ref.to_json(),
                    "origin_refs": [],
                }
                base.update(overrides)
                return base

            good = _zip({"skill.json": json.dumps(manifest()).encode(), "SKILL.md": body_md})
            result = importer.import_bundle(_command(runtime, good, "NATIVE"), good, caller=trusted_caller(), command_id="n1")
            assert result.revision.entry_id == "native-one" and result.lock["complete"] is True
            assert [n["id"] for n in result.lock["nodes"]] == ["session_history_search"] and len(result.lock["edges"]) == 1
            assert result.revision.body["origin_refs"][0] == _artifact(good).to_json()
            # A file the manifest does not list, or a wrong hash: refused by name.
            unlisted = _zip({"skill.json": json.dumps(manifest()).encode(), "SKILL.md": body_md, "extra.md": b"?"})
            with pytest.raises(ArpError) as refused:
                importer.import_bundle(_command(runtime, unlisted, "NATIVE"), unlisted, caller=trusted_caller(), command_id="n2")
            assert refused.value.code == "SKILL_MANIFEST_CONFLICT"
            wrong = _zip({"skill.json": json.dumps(manifest()).encode(), "SKILL.md": b"# changed\n"})
            with pytest.raises(ArpError) as refused:
                importer.import_bundle(_command(runtime, wrong, "NATIVE"), wrong, caller=trusted_caller(), command_id="n3")
            assert refused.value.code == "SOURCE_HASH_CONFLICT"
            # Frontmatter and skill.json disagree on the name.
            both = _zip({"skill.json": json.dumps(manifest(files=[{"relative_path": "SKILL.md", "size_bytes": len(FRONT.encode()), "sha256": hashlib.sha256(FRONT.encode()).hexdigest(), "role": "INSTRUCTIONS"}])).encode(), "SKILL.md": FRONT.encode()})
            with pytest.raises(ArpError) as refused:
                importer.import_bundle(_command(runtime, both, "NATIVE"), both, caller=trusted_caller(), command_id="n4")
            assert refused.value.code == "SKILL_MANIFEST_CONFLICT"
            # An exact ref that does not exist keeps the lock incomplete (QUARANTINED stays).
            ghost = Pin("skill", "ghost", 3, digest("ghost"))
            dep = _zip({"skill.json": json.dumps(manifest(skill_id="needs-ghost", name="needs-ghost", required_skill_refs=[ghost.to_json()])).encode(), "SKILL.md": body_md})
            result = importer.import_bundle(_command(runtime, dep, "NATIVE"), dep, caller=trusted_caller(), command_id="n5")
            assert result.lock["complete"] is False and result.lock["unresolved"][0]["request_ref"] == ghost.to_json()
            assert result.activation.state == "QUARANTINED"
            # Diamond: the same logical skill required at two versions.
            first = cat.latest_revision(service.connection, service.namespace_id, "SKILL", "native-one").pin
            fake_v2 = Pin("skill", "native-one", 2, digest("v2"))
            mid = _zip({"skill.json": json.dumps(manifest(skill_id="mid", name="mid", required_skill_refs=[fake_v2.to_json()])).encode(), "SKILL.md": body_md})
            importer.import_bundle(_command(runtime, mid, "NATIVE"), mid, caller=trusted_caller(), command_id="n6")
            mid_pin = cat.latest_revision(service.connection, service.namespace_id, "SKILL", "mid").pin
            top = _zip({"skill.json": json.dumps(manifest(skill_id="top", name="top", required_skill_refs=[first.to_json(), mid_pin.to_json()])).encode(), "SKILL.md": body_md})
            with pytest.raises(ArpError) as refused:
                importer.import_bundle(_command(runtime, top, "NATIVE"), top, caller=trusted_caller(), command_id="n7")
            assert refused.value.code == "DEPENDENCY_DIAMOND_CONFLICT"
            # A SCRIPT implementation must point at a SCRIPT-role file and a registered runner.
            script = _zip({"skill.json": json.dumps(manifest(skill_id="scripted", name="scripted", implementation={"kind": "SCRIPT", "script_path": "SKILL.md", "runner_ref": report.provider_ref.to_json(), "argv_template": ["python", "{input_json}", "{output_json}"]})).encode(), "SKILL.md": body_md})
            with pytest.raises(ArpError) as refused:
                importer.import_bundle(_command(runtime, script, "NATIVE"), script, caller=trusted_caller(), command_id="n8")
            assert refused.value.code == "SKILL_MANIFEST_CONFLICT"

    _run(case())


# ---- RP-C2 审阅阻断项回归 ----------------------------------------------------------------


def _stored_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def _md(name: str, body: str = "x") -> bytes:
    return f"---\nname: {name}\ndescription: b\n---\n{body}\n".encode()


def test_reimporting_the_same_bundle_never_bumps_the_version(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            importer, service = runtime.arp.skills, runtime.arp.catalogue
            v1 = _zip({"SKILL.md": _md("same-skill", "one")})
            v2 = _zip({"SKILL.md": _md("same-skill", "two")})
            assert importer.import_bundle(_command(runtime, v1), v1, caller=trusted_caller(), command_id="s1").revision.revision == 1
            assert importer.import_bundle(_command(runtime, v2), v2, caller=trusted_caller(), command_id="s2").revision.revision == 2
            epoch = service.epoch()
            for command in ("s2", "s3", "s4"):  # re-sent command and fresh commands with the same bytes
                again = importer.import_bundle(_command(runtime, v2), v2, caller=trusted_caller(), command_id=command)
                assert again.replayed and again.revision.revision == 2
            assert service.epoch() == epoch
            # The same command id with another bundle is a conflict, not a third revision.
            with pytest.raises(ArpError) as refused:
                importer.import_bundle(_command(runtime, v1), v1, caller=trusted_caller(), command_id="s2")
            assert refused.value.code == "SOURCE_HASH_CONFLICT"
            # A re-sent command replays even after the catalogue moved on.
            stale = {**_command(runtime, v2), "expected_catalogue_revision": 1}
            assert importer.import_bundle(stale, v2, caller=trusted_caller(), command_id="s2").replayed
            with pytest.raises(ArpError) as moved:
                importer.import_bundle(stale, v2, caller=trusted_caller(), command_id="s9")
            assert moved.value.code == "EXPECTED_REVISION_MISMATCH"

    _run(case())


def test_skill_without_a_complete_lock_cannot_enter_trial(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            importer, service = runtime.arp.skills, runtime.arp.catalogue
            ghost = Pin("skill", "ghost", 3, digest("ghost"))
            report = runtime.arp.bootstrap
            body = "# 说明\n".encode()
            manifest = {
                "schema_version": 1, "skill_id": "needs-ghost", "version": 1, "name": "needs-ghost", "description": "缺依赖。",
                "files": [{"relative_path": "SKILL.md", "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "role": "INSTRUCTIONS"}],
                "instructions_path": "SKILL.md",
                "input_schema_ref": report.instructions_schema_ref.to_json(), "output_schema_ref": report.instructions_schema_ref.to_json(),
                "capability_ref": report.instructions_capability_ref.to_json(),
                "required_tool_refs": [], "required_skill_refs": [ghost.to_json()],
                "implementation": {"kind": "INSTRUCTIONS"},
                "requested_permission_policy_ref": Pin("policy", "p", 0, digest("p")).to_json(),
                "verification_policy_ref": report.verification_policy_ref.to_json(),
                "origin_refs": [],
            }
            data = _zip({"skill.json": json.dumps(manifest).encode(), "SKILL.md": body})
            result = importer.import_bundle(_command(runtime, data, "NATIVE"), data, caller=trusted_caller(), command_id="lock-1")
            assert result.lock["complete"] is False
            with pytest.raises(ArpError) as refused:
                service.transition(result.revision.pin, state="TRIAL", caller=trusted_caller(), command_id="trial-1")
            assert refused.value.code == "DEPENDENCY_UNRESOLVED"
            assert cat.read_activation(service.connection, service.namespace_id, "SKILL", "needs-ghost", 1).state == "QUARANTINED"
            # A skill with a complete (empty) lock does enter TRIAL.
            ok = _zip({"SKILL.md": _md("lonely")})
            lonely = importer.import_bundle(_command(runtime, ok), ok, caller=trusted_caller(), command_id="lock-2")
            assert service.transition(lonely.revision.pin, state="TRIAL", caller=trusted_caller(), command_id="trial-2").state == "TRIAL"

    _run(case())


def _corrupt_crc(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.infolist()[0]
    body = bytearray(data)
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    body[start] ^= 0xFF  # flip one stored byte: the CRC no longer matches
    return bytes(body)


def _flag(data: bytes, flag: int) -> bytes:
    """Set a general-purpose flag bit (e.g. 0x01 encrypted) in both headers of the first entry."""
    body = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.infolist()[0]
        central = archive.start_dir
    for offset in (info.header_offset + 6, central + 8):
        body[offset : offset + 2] = ((int.from_bytes(body[offset : offset + 2], "little")) | flag).to_bytes(2, "little")
    return bytes(body)


def _method(data: bytes, method: int) -> bytes:
    body = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.infolist()[0]
        central = archive.start_dir
    for offset in (info.header_offset + 8, central + 10):
        body[offset : offset + 2] = method.to_bytes(2, "little")
    return bytes(body)


def _with_comment(data: bytes) -> bytes:
    buffer = io.BytesIO(data)
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.comment = b"hidden payload"
    return buffer.getvalue()


def _with_gap(data: bytes) -> bytes:
    """Insert undeclared bytes between the last entry and the central directory."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        central = archive.start_dir
    gap = b"\0" * 16
    body = bytearray(data[:central] + gap + data[central:])
    eocd = len(body) - 22
    body[eocd + 16 : eocd + 20] = (central + len(gap)).to_bytes(4, "little")  # the end record still finds the directory
    return bytes(body)


def _dir_with_payload() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("SKILL.md", _md("a"))
        archive.writestr("docs/", b"hidden")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda d: b"#!/bin/sh\nevil\n" + d, "SKILL_PATH_INVALID"),  # prepended script (the zip still opens)
        (lambda d: d + b"trailing", "SKILL_PATH_INVALID"),  # bytes after the end record
        (_with_comment, "SKILL_PATH_INVALID"),
        (_with_gap, "SKILL_PATH_INVALID"),
        (lambda d: _flag(d, 0x01), "SKILL_PATH_INVALID"),  # encrypted entry
        (lambda d: _method(d, 12), "SKILL_PATH_INVALID"),  # bzip2
        (_corrupt_crc, "SOURCE_HASH_CONFLICT"),
        (lambda d: d[: len(d) // 2], "SKILL_PATH_INVALID"),  # truncated archive
        (lambda d: _dir_with_payload(), "SKILL_PATH_INVALID"),
    ],
)
def test_hidden_or_unreadable_bundle_content_is_refused_by_name(mutate, code: str) -> None:  # type: ignore[no-untyped-def]
    data = _stored_zip({"SKILL.md": _md("a"), "notes/b.md": b"bb"})
    with pytest.raises(ArpError) as refused:
        inspect_bundle(mutate(data))
    assert refused.value.code == code


def test_directory_entries_are_path_checked_and_empty() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("SKILL.md", _md("a"))
        archive.writestr("docs/", b"")
    assert [f.path for f in inspect_bundle(buffer.getvalue()).files] == ["SKILL.md"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("SKILL.md", _md("a"))
        archive.writestr("../up/", b"")
    with pytest.raises(ArpError) as refused:
        inspect_bundle(buffer.getvalue())
    assert refused.value.code == "SKILL_PATH_INVALID"


def test_deeply_nested_frontmatter_is_refused_by_name() -> None:
    text = "---\nname: a\ndescription: b\nmetadata: " + "[" * 5000 + "]" * 5000 + "\n---\n"
    with pytest.raises(ArpError) as refused:
        parse_frontmatter(text.encode())
    assert refused.value.code == "SKILL_FRONTMATTER_INVALID"
