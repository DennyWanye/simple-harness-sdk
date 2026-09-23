# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Skill bundle import and dependency locks (§9.1–9.5, SKILL-CATALOGUE §1–§2).

A bundle is one zip whose bytes are the ``artifact`` pin's content. It is inspected
read-only (never executed): bounded counts and sizes, no path escape / symlink / case
collision / reserved name, every file hashed from its exact bytes. Two formats:

* ``NATIVE``: a root ``skill.json`` that is a complete ``Skill`` definition whose
  ``files`` must match the bundle exactly (``skill.json`` itself is not listed) and
  whose schema / capability refs resolve in the namespace catalogue.
* ``SKILL_MD``: a root ``SKILL.md`` whose frontmatter (safe YAML, strict subset) becomes
  a new **INSTRUCTIONS** candidate with zero execution authority; a ``scripts/`` folder
  is an asset, never an executable.

The definition enters the catalogue QUARANTINED with a dependency-lock candidate; an
incomplete lock keeps it there (``DEPENDENCY_UNRESOLVED``) until every exact ref exists.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import catalogue as cat
from .codec import check
from .errors import ArpError
from .pins import Pin
from .ports import TrustedCaller
from .rules import safe_skill_path, validate_bundle_paths
from .store import _json_column
from .strict import canonical, digest, plain

MAX_FILES = 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SKILL_MD_BYTES = 128 * 1024
MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_LOCK_NODES = 128
MAX_LOCK_DEPTH = 16
DETAILS_PAGE_ITEMS = 64
DETAILS_PAGE_BYTES = 65536
SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SKILL_JSON = "skill.json"
SKILL_MD = "SKILL.md"
INSTRUCTIONS_CAPABILITY_ID = "sdk.skill.instructions"
INSTRUCTIONS_SCHEMA_ID = "sdk.skill.instructions.io"


# ---- bundle inspection ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Bundle:
    digest: str
    files: tuple[BundleFile, ...]  # every regular file, sorted by path (skill.json included)
    skill_json: Mapping[str, Any] | None
    skill_md: bytes | None

    def by_path(self) -> dict[str, BundleFile]:
        return {f.path: f for f in self.files}


def _strict_json(data: bytes) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ArpError("SKILL_MANIFEST_CONFLICT", f"duplicate key {key!r} in {SKILL_JSON}")
            out[key] = value
        return out

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates)
    except UnicodeDecodeError as error:
        raise ArpError("SKILL_MANIFEST_CONFLICT", f"{SKILL_JSON} is not UTF-8") from error
    except ValueError as error:
        raise ArpError("INVALID_JSON", f"{SKILL_JSON}: {error}") from error


_LOCAL_SIG = b"PK\x03\x04"
_CENTRAL_SIG = b"PK\x01\x02"
_EOCD_SIG = b"PK\x05\x06"
_DESCRIPTOR_SIG = b"PK\x07\x08"
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, OSError, ValueError)


def _verify_layout(data: bytes, archive: zipfile.ZipFile) -> None:
    """Every byte of the archive is accounted for: local headers back to back from
    offset 0, then the central directory, then an end record with no comment. Prepended
    scripts, trailing payloads, comments and gaps are undeclared payload (§9.4)."""

    if archive.comment:
        raise ArpError("SKILL_PATH_INVALID", "archive comment is undeclared payload")
    eocd = data.rfind(_EOCD_SIG)
    if eocd < 0 or eocd + 22 != len(data):
        raise ArpError("SKILL_PATH_INVALID", "bytes after the end-of-central-directory record are undeclared payload")
    infos = sorted(archive.infolist(), key=lambda i: i.header_offset)
    position = 0
    for info in infos:
        if info.header_offset != position:
            raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: undeclared bytes before its local header")
        if data[position : position + 4] != _LOCAL_SIG or len(data) < position + 30:
            raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: local header missing")
        name_length = int.from_bytes(data[position + 26 : position + 28], "little")
        extra_length = int.from_bytes(data[position + 28 : position + 30], "little")
        if data[position + 30 : position + 30 + name_length] != info.filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437", "replace"):
            raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: local and central names differ")
        position += 30 + name_length + extra_length + info.compress_size
        if info.flag_bits & 0x08:  # data descriptor, with or without its optional signature
            position += 16 if data[position : position + 4] == _DESCRIPTOR_SIG else 12
    if position != archive.start_dir:
        raise ArpError("SKILL_PATH_INVALID", "undeclared bytes between the entries and the central directory")
    central = sum(46 + len(i.filename.encode("utf-8" if i.flag_bits & 0x800 else "cp437", "replace")) + len(i.extra) + len(i.comment) for i in infos)
    if archive.start_dir + central != eocd:
        raise ArpError("SKILL_PATH_INVALID", "undeclared bytes inside the central directory")


def inspect_bundle(data: bytes) -> Bundle:
    """Read-only, bounded inspection of one zip; every refusal is named (§9.4)."""

    bundle_digest = hashlib.sha256(data).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except _ZIP_ERRORS as error:
        raise ArpError("SKILL_PATH_INVALID", f"not a readable zip archive: {type(error).__name__}") from error
    files: list[BundleFile] = []
    total = 0
    skill_json: Mapping[str, Any] | None = None
    skill_md: bytes | None = None
    with archive:
        _verify_layout(data, archive)
        entries = archive.infolist()
        infos = [i for i in entries if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise ArpError("ARRAY_LIMIT", f"bundle lists {len(infos)} files (max {MAX_FILES})")
        seen_raw: set[str] = set()
        for info in entries:
            if info.flag_bits & 0x01:
                raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: encrypted entries are not allowed")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: only STORED/DEFLATED entries are allowed")
            if info.is_dir():
                # A directory entry is still a path (escapes are refused) and carries no bytes.
                safe_skill_path(info.filename.rstrip("/"))
                if info.file_size or info.compress_size:
                    raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: directory entry carries undeclared payload")
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode and not stat.S_ISREG(mode):
                raise ArpError("SKILL_PATH_INVALID", f"{info.filename}: only regular files are allowed (no symlinks)")
            if info.filename in seen_raw:
                raise ArpError("SKILL_PATH_COLLISION", f"{info.filename} listed twice")
            seen_raw.add(info.filename)
            path = safe_skill_path(info.filename)
            if info.file_size > MAX_FILE_BYTES:
                raise ArpError("ITEM_TOO_LARGE", f"{path}: {info.file_size} bytes (max {MAX_FILE_BYTES})")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArpError("ITEM_TOO_LARGE", f"bundle exceeds {MAX_TOTAL_BYTES} bytes unpacked")
            hasher = hashlib.sha256()
            read = 0
            payload = bytearray() if path in (SKILL_JSON, SKILL_MD) else None
            try:
                with archive.open(info) as handle:
                    while True:
                        chunk = handle.read(65536)
                        if not chunk:
                            break
                        read += len(chunk)
                        if read > info.file_size:
                            raise ArpError("ITEM_TOO_LARGE", f"{path}: more bytes than declared (zip bomb)")
                        hasher.update(chunk)
                        if payload is not None:
                            payload.extend(chunk)
            except ArpError:
                raise
            except _ZIP_ERRORS as error:  # CRC mismatch, truncated stream, unsupported method…
                raise ArpError("SOURCE_HASH_CONFLICT", f"{path}: entry cannot be read intact ({type(error).__name__})") from error
            if read != info.file_size:
                raise ArpError("SOURCE_HASH_CONFLICT", f"{path}: declared size differs from content")
            files.append(BundleFile(path, read, hasher.hexdigest()))
            if path == SKILL_JSON:
                skill_json = _strict_json(bytes(payload or b""))
            elif path == SKILL_MD:
                if read > MAX_SKILL_MD_BYTES:
                    raise ArpError("ITEM_TOO_LARGE", f"{SKILL_MD} exceeds {MAX_SKILL_MD_BYTES} bytes")
                skill_md = bytes(payload or b"")
    validate_bundle_paths(f.path for f in files)
    return Bundle(bundle_digest, tuple(sorted(files, key=lambda f: f.path)), skill_json, skill_md)


# ---- SKILL.md frontmatter (safe YAML subset) ------------------------------------------------


def _strict_loader():  # type: ignore[no-untyped-def]
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - environment without the extra
        raise ArpError("SOURCE_UNAVAILABLE", "PyYAML is required for SKILL.md import (install the 'skill-import' extra)") from error

    class StrictLoader(yaml.SafeLoader):
        """SafeLoader that refuses aliases, anchors, tags, merge keys, duplicate or
        non-string keys and implicit timestamps (§9.3)."""

        def fetch_alias(self):  # type: ignore[no-untyped-def]
            raise ArpError("SKILL_FRONTMATTER_INVALID", "YAML aliases are not allowed")

        def fetch_anchor(self):  # type: ignore[no-untyped-def]
            raise ArpError("SKILL_FRONTMATTER_INVALID", "YAML anchors are not allowed")

        def fetch_tag(self):  # type: ignore[no-untyped-def]
            raise ArpError("SKILL_FRONTMATTER_INVALID", "YAML tags are not allowed")

        def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
            if not isinstance(node, yaml.MappingNode):
                raise ArpError("SKILL_FRONTMATTER_INVALID", "frontmatter must be a mapping")
            seen: set[str] = set()
            for key_node, _ in node.value:
                if key_node.tag != "tag:yaml.org,2002:str":
                    raise ArpError("SKILL_FRONTMATTER_INVALID", "mapping keys must be strings")
                if key_node.value == "<<":
                    raise ArpError("SKILL_FRONTMATTER_INVALID", "merge keys are not allowed")
                if key_node.value in seen:
                    raise ArpError("SKILL_FRONTMATTER_INVALID", f"duplicate key {key_node.value!r}")
                seen.add(key_node.value)
            return super().construct_mapping(node, deep=deep)

    # No implicit timestamps / binary / python objects: only the plain scalars stay.
    StrictLoader.yaml_implicit_resolvers = {
        first: [(tag, regexp) for tag, regexp in resolvers if tag in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:null")]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    return yaml, StrictLoader


def parse_frontmatter(skill_md: bytes) -> dict[str, Any]:
    """The frontmatter of one SKILL.md as the allowed, typed subset (§9.3)."""

    try:
        text = skill_md.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArpError("SKILL_FRONTMATTER_INVALID", "SKILL.md is not UTF-8") from error
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise ArpError("SKILL_FRONTMATTER_INVALID", "frontmatter must start on the first line with ---")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r") == "---"), None)
    if end is None:
        raise ArpError("SKILL_FRONTMATTER_INVALID", "frontmatter has no closing ---")
    block = "\n".join(lines[1:end])
    if len(block.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise ArpError("ITEM_TOO_LARGE", f"frontmatter exceeds {MAX_FRONTMATTER_BYTES} bytes")
    yaml, loader = _strict_loader()
    try:
        value = yaml.load(block, Loader=loader)  # noqa: S506 - strict SafeLoader subclass
    except ArpError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError) as error:
        raise ArpError("SKILL_FRONTMATTER_INVALID", f"frontmatter is not valid YAML: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise ArpError("SKILL_FRONTMATTER_INVALID", "frontmatter must be a mapping")
    unknown = set(value) - FRONTMATTER_KEYS
    if unknown:
        raise ArpError("SKILL_FRONTMATTER_INVALID", f"unknown frontmatter keys: {sorted(unknown)}")
    name = value.get("name")
    description = value.get("description")
    if not isinstance(name, str) or not SKILL_NAME.match(name) or len(name) > 64:
        raise ArpError("SKILL_FRONTMATTER_INVALID", "name must match [a-z0-9]+(-[a-z0-9]+)* (1–64 chars; an ARP portability rule)")
    if not isinstance(description, str) or not description.strip():
        raise ArpError("SKILL_FRONTMATTER_INVALID", "description must be a non-empty string")
    for key in ("license", "compatibility"):
        if key in value and not isinstance(value[key], str):
            raise ArpError("SKILL_FRONTMATTER_INVALID", f"{key} must be a string")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
        raise ArpError("SKILL_FRONTMATTER_INVALID", "metadata must map strings to strings")
    tools = value.get("allowed-tools", [])
    if isinstance(tools, str):
        tools = tools.split()
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        raise ArpError("SKILL_FRONTMATTER_INVALID", "allowed-tools must be a list of strings")
    return {"name": name, "description": description, "license": value.get("license"), "compatibility": value.get("compatibility"), "metadata": dict(metadata), "allowed-tools": list(tools)}


# ---- definitions --------------------------------------------------------------------------


def _role_for(path: str) -> str:
    if path == SKILL_MD:
        return "INSTRUCTIONS"
    return "REFERENCE" if path.lower().endswith((".md", ".txt")) else "ASSET"


@dataclass(frozen=True, slots=True)
class SkillImportResult:
    revision: cat.RevisionRow
    activation: cat.ActivationRow
    lock: Mapping[str, Any]
    bundle_digest: str
    frontmatter: Mapping[str, Any] | None
    replayed: bool


@dataclass(slots=True)
class SkillImporter:
    """``import_skill_bundle`` + ``resolve_skill_dependencies`` + details pages."""

    catalogue: cat.CatalogueService
    bundle_dir: Path
    instructions_capability: Pin
    instructions_schema: Pin
    verification_policy: Pin
    clock_ms: Callable[[], int]

    # -- import --

    def import_bundle(self, command: Mapping[str, Any], data: bytes, *, caller: TrustedCaller, command_id: str, run_id: str | None = None) -> SkillImportResult:
        value = check("SkillInstallCommand", plain(command))
        artifact = Pin.from_json(value["bundle_artifact_ref"])
        if hashlib.sha256(data).hexdigest() != artifact.content_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", "bundle bytes do not match the artifact pin")
        if Pin.from_json(value["scope_ref"]) != self.catalogue.scope.pin:
            raise ArpError("REF_OUTSIDE_SCOPE", "the install scope is not this namespace")
        # Same command id with another bundle: a conflict, never a second install.
        previous = self.catalogue.connection.execute(
            "SELECT bundle_hash FROM arp_skill_import_commands WHERE namespace_id=? AND command_id=?", (self.catalogue.namespace_id, command_id)
        ).fetchone()
        same_command = previous is not None and str(previous[0]) == artifact.content_hash
        if previous is not None and not same_command:
            raise ArpError("SOURCE_HASH_CONFLICT", "command id re-sent with another bundle")
        # A new command was prepared against the current catalogue; a re-sent one replays.
        if not same_command and int(value["expected_catalogue_revision"]) != self.catalogue.epoch():
            raise ArpError("EXPECTED_REVISION_MISMATCH", "the catalogue moved since the command was prepared")
        bundle = inspect_bundle(data)
        # A NATIVE bundle's SKILL.md is plain instructions unless it carries a frontmatter
        # block; a SKILL_MD bundle must carry one (§9.2).
        has_frontmatter = bundle.skill_md is not None and (value["format"] == "SKILL_MD" or bundle.skill_md.startswith(b"---"))
        frontmatter = parse_frontmatter(bundle.skill_md) if has_frontmatter else None  # type: ignore[arg-type]
        if value["format"] == "NATIVE":
            if bundle.skill_json is None:
                raise ArpError("SKILL_MANIFEST_CONFLICT", f"NATIVE bundle has no root {SKILL_JSON}")
            definition = self._native_definition(bundle, artifact)
            if frontmatter is not None and (frontmatter["name"] != definition["name"] or frontmatter["description"] != definition["description"]):
                raise ArpError("SKILL_MANIFEST_CONFLICT", f"{SKILL_MD} frontmatter disagrees with {SKILL_JSON}")
        else:
            if bundle.skill_md is None or frontmatter is None:
                raise ArpError("SKILL_FRONTMATTER_INVALID", f"SKILL_MD bundle has no root {SKILL_MD}")
            if bundle.skill_json is not None:
                raise ArpError("SKILL_MANIFEST_CONFLICT", f"SKILL_MD bundle must not carry a {SKILL_JSON}; import it as NATIVE")
            definition = self._instructions_definition(bundle, frontmatter, artifact)
        existing = cat.latest_revision(self.catalogue.connection, self.catalogue.namespace_id, "SKILL", definition["skill_id"])
        if existing is not None:
            definition["version"] = existing.revision  # compare at the latest revision's own number
        replay = existing is not None and existing.content_hash == digest(check("Skill", definition))
        if not replay:
            definition["version"] = 1 if existing is None else existing.revision + 1
        self._store_bundle(bundle.digest, data)
        revision, activation = self.catalogue.register(
            "SKILL", definition, entry_id=definition["skill_id"], caller=caller, command_id=command_id, bundle_root_ref=artifact, run_id=run_id,
        )
        lock = self.resolve_dependencies(revision, source_receipt_ref=revision.source_receipt_ref)
        if previous is None:
            with self.catalogue.uow.database.transaction() as txn:
                txn.execute(
                    "INSERT INTO arp_skill_import_commands(namespace_id,command_id,bundle_hash,skill_id,skill_revision) VALUES (?,?,?,?,?)",
                    (self.catalogue.namespace_id, command_id, artifact.content_hash, revision.entry_id, revision.revision),
                )
        return SkillImportResult(revision, activation, lock, bundle.digest, frontmatter, replayed=replay)

    def _native_definition(self, bundle: Bundle, artifact: Pin) -> dict[str, Any]:
        body = dict(plain(bundle.skill_json))
        body.setdefault("schema_version", 1)
        body.setdefault("version", 1)
        body.setdefault("origin_refs", [])
        checked = check("Skill", body)
        listed = {f["relative_path"]: f for f in checked["files"]}
        actual = {p: f for p, f in bundle.by_path().items() if p != SKILL_JSON}
        if set(listed) != set(actual):
            missing = sorted(set(actual) - set(listed))
            extra = sorted(set(listed) - set(actual))
            raise ArpError("SKILL_MANIFEST_CONFLICT", f"files differ from the bundle (unlisted={missing}, absent={extra})")
        for path, entry in listed.items():
            real = actual[path]
            if int(entry["size_bytes"]) != real.size_bytes or entry["sha256"] != real.sha256:
                raise ArpError("SOURCE_HASH_CONFLICT", f"{path}: manifest size/hash differ from the bundle bytes")
        if checked["instructions_path"] not in actual:
            raise ArpError("SKILL_MANIFEST_CONFLICT", "instructions_path is not in the bundle")
        connection = self.catalogue.connection
        for key in ("input_schema_ref", "output_schema_ref", "capability_ref"):
            cat.resolve_pin(connection, self.catalogue.namespace_id, Pin.from_json(checked[key]))
        implementation = checked["implementation"]
        if implementation["kind"] == "SCRIPT":
            if implementation["script_path"] not in actual or listed[implementation["script_path"]]["role"] != "SCRIPT":
                raise ArpError("SKILL_MANIFEST_CONFLICT", "script_path must be a bundle file with role SCRIPT")
            cat.resolve_pin(connection, self.catalogue.namespace_id, Pin.from_json(implementation["runner_ref"]))
        origin = [o for o in checked["origin_refs"] if o != artifact.to_json()]
        checked["origin_refs"] = [artifact.to_json(), *origin][:128]
        return checked

    def _instructions_definition(self, bundle: Bundle, frontmatter: Mapping[str, Any], artifact: Pin) -> dict[str, Any]:
        files = [{"relative_path": f.path, "size_bytes": f.size_bytes, "sha256": f.sha256, "role": _role_for(f.path)} for f in bundle.files]
        definition = {
            "schema_version": 1,
            "skill_id": frontmatter["name"],
            "version": 1,
            "name": frontmatter["name"],
            "description": str(frontmatter["description"])[:16000],
            "files": files,
            "instructions_path": SKILL_MD,
            "input_schema_ref": self.instructions_schema.to_json(),
            "output_schema_ref": self.instructions_schema.to_json(),
            "capability_ref": self.instructions_capability.to_json(),
            "required_tool_refs": [],   # allowed-tools is a suggestion, never an authority (§9.3)
            "required_skill_refs": [],
            "implementation": {"kind": "INSTRUCTIONS"},
            "requested_permission_policy_ref": Pin("policy", "builtin:skill-no-authority", 0, digest("skill-no-authority")).to_json(),
            "verification_policy_ref": self.verification_policy.to_json(),
            "origin_refs": [artifact.to_json(), Pin("source", f"frontmatter:{frontmatter['name']}", 0, digest(frontmatter)).to_json()],
        }
        return check("Skill", definition)

    # -- bundle bytes --

    def _bundle_path(self, bundle_digest: str) -> Path:
        return self.bundle_dir / f"{bundle_digest}.zip"

    def _store_bundle(self, bundle_digest: str, data: bytes) -> None:
        target = self._bundle_path(bundle_digest)
        if target.exists():
            return
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".zip.part")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    def read_file(self, skill: cat.RevisionRow, relative_path: str) -> bytes:
        """Exact bytes of one bundle file, verified against the definition's hash."""

        if skill.entry_kind != "SKILL" or skill.bundle_root_ref is None:
            raise ArpError("REF_KIND_MISMATCH", "not a skill revision with a bundle")
        entry = next((f for f in skill.body["files"] if f["relative_path"] == relative_path), None)
        if entry is None:
            raise ArpError("SKILL_PATH_INVALID", f"{relative_path} is not a file of this skill")
        path = self._bundle_path(skill.bundle_root_ref.content_hash)
        if not path.exists():
            raise ArpError("JOURNAL_SOURCE_UNAVAILABLE", "bundle bytes are not available on this root")
        with zipfile.ZipFile(path) as archive:
            data = archive.read(relative_path)
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != int(entry["size_bytes"]):
            raise ArpError("SOURCE_HASH_CONFLICT", f"{relative_path}: stored bytes differ from the definition")
        return data

    # -- dependency lock (§9.5) --

    def resolve_dependencies(self, skill: cat.RevisionRow, *, source_receipt_ref: Pin) -> dict[str, Any]:
        """Exact-ref closure: nodes/edges/unresolved, complete only when every ref exists;
        diamonds (same logical id, two versions) and cycles are refused, never auto-picked."""

        connection = self.catalogue.connection
        namespace = self.catalogue.namespace_id
        root = skill.pin
        nodes: dict[tuple[str, str, int, str], Pin] = {}
        logical: dict[tuple[str, str], Pin] = {}
        edges: list[tuple[Pin, Pin]] = []
        unresolved: list[dict[str, Any]] = []
        adjacency: dict[str, list[str]] = {}

        def visit(parent: cat.RevisionRow, depth: int) -> None:
            if depth > MAX_LOCK_DEPTH:
                raise ArpError("ARRAY_LIMIT", f"dependency depth exceeds {MAX_LOCK_DEPTH}")
            children = [Pin.from_json(r) for r in parent.body.get("required_tool_refs", [])] + [Pin.from_json(r) for r in parent.body.get("required_skill_refs", [])]
            for child in children:
                key = (child.kind, child.id, child.revision, child.content_hash)
                seen = logical.get((child.kind, child.id))
                if seen is not None and seen != child:
                    raise ArpError("DEPENDENCY_DIAMOND_CONFLICT", f"{child.kind} {child.id} is required at two versions")
                edges.append((parent.pin, child))
                adjacency.setdefault(parent.pin.id, []).append(child.id)
                if key in nodes:
                    continue
                try:
                    resolved = cat.resolve_pin(connection, namespace, child)
                except ArpError as error:
                    unresolved.append({"request_ref": child.to_json(), "reason_code": error.code})
                    nodes[key] = child
                    logical[(child.kind, child.id)] = child
                    continue
                nodes[key] = child
                logical[(child.kind, child.id)] = child
                if len(nodes) > MAX_LOCK_NODES:
                    raise ArpError("ARRAY_LIMIT", f"dependency nodes exceed {MAX_LOCK_NODES}")
                if child.kind == "skill":
                    if resolved.pin == root:
                        raise ArpError("DEPENDENCY_CYCLE", "skill depends on itself")
                    visit(resolved, depth + 1)

        adjacency[root.id] = []
        visit(skill, 1)
        # Cycle check over the logical graph (ids), root included.
        def walk(node: str, path: tuple[str, ...]) -> None:
            if node in path:
                raise ArpError("DEPENDENCY_CYCLE", f"cycle through {node}")
            for child in adjacency.get(node, ()):
                walk(child, path + (node,))

        walk(root.id, ())
        ordered_nodes = [p.to_json() for _, p in sorted(nodes.items())]
        ordered_edges = [{"parent": a.to_json(), "child": b.to_json()} for a, b in edges]
        lock = {
            "schema_version": 1,
            "skill_ref": root.to_json(),
            "scope_ref": self.catalogue.scope.pin.to_json(),
            "nodes": ordered_nodes,
            "edges": ordered_edges,
            "unresolved": unresolved,
            "complete": not unresolved,
            "lock_hash": "",
        }
        lock["lock_hash"] = digest({k: v for k, v in lock.items() if k != "lock_hash"})
        value = check("DependencyLock", lock)
        with self.catalogue.uow.database.transaction() as txn:
            existing = txn.execute(
                "SELECT lock_json FROM arp_dependency_locks WHERE skill_id=? AND skill_revision=? AND lock_hash=?", (skill.entry_id, skill.revision, value["lock_hash"])
            ).fetchone()
            if existing is None:
                txn.execute(
                    "INSERT INTO arp_dependency_locks(skill_id,skill_revision,lock_hash,lock_json,complete,source_receipt_ref_json) VALUES (?,?,?,?,?,?)",
                    (skill.entry_id, skill.revision, value["lock_hash"], _json_column(value), 1 if value["complete"] else 0, _json_column(source_receipt_ref.to_json())),
                )
        return value

    def latest_lock(self, skill: cat.RevisionRow) -> Mapping[str, Any] | None:
        raw = self.catalogue.connection.execute(
            "SELECT lock_json FROM arp_dependency_locks WHERE skill_id=? AND skill_revision=? ORDER BY rowid DESC LIMIT 1", (skill.entry_id, skill.revision)
        ).fetchone()
        return None if raw is None else json.loads(str(raw[0]))

    @staticmethod
    def lock_pin(lock: Mapping[str, Any]) -> Pin:
        return Pin("dependency_lock", f"{lock['skill_ref']['id']}@{lock['skill_ref']['revision']}", 0, str(lock["lock_hash"]))

    # -- details page (§9.10) --

    def details(self, command: Mapping[str, Any]) -> dict[str, Any]:
        value = check("SkillDetailsCommand", plain(command))
        skill = cat.resolve_pin(self.catalogue.connection, self.catalogue.namespace_id, Pin.from_json(value["skill_ref"]))
        if skill.entry_kind != "SKILL":
            raise ArpError("CATALOGUE_KIND_MISMATCH", "not a skill")
        lock = self.latest_lock(skill)
        if lock is None:
            raise ArpError("DEPENDENCY_UNRESOLVED", "skill has no dependency lock yet")
        files = list(skill.body["files"])
        offset = 0
        if value["cursor"] is not None:
            try:
                cursor_hash, offset_text = str(value["cursor"]).split(":", 1)
                offset = int(offset_text)
            except ValueError as error:
                raise ArpError("CURSOR_UNKNOWN") from error
            if cursor_hash != skill.content_hash[:16] or offset > len(files):
                raise ArpError("CURSOR_UNKNOWN", "cursor belongs to another skill revision")
        limit = min(int(value["limit"]), DETAILS_PAGE_ITEMS)
        chosen = files[offset : offset + limit]
        while True:
            end = offset + len(chosen)
            page = {
                "schema_version": 1,
                "skill_ref": skill.pin.to_json(),
                "bundle_digest": skill.bundle_root_ref.content_hash if skill.bundle_root_ref is not None else digest(skill.body),
                "metadata_ref": Pin("artifact", f"skill-metadata:{skill.entry_id}:{skill.revision}", skill.revision, skill.content_hash).to_json(),
                "dependency_lock_ref": self.lock_pin(lock).to_json(),
                "files": chosen,
                "has_more": end < len(files),
                "next_cursor": f"{skill.content_hash[:16]}:{end}" if end < len(files) else None,
                "body_bytes": 0,
            }
            for _ in range(4):
                size = len(canonical(page))
                if size == page["body_bytes"]:
                    break
                page["body_bytes"] = size
            if page["body_bytes"] <= DETAILS_PAGE_BYTES or not chosen:
                break
            chosen = chosen[:-1]
        if not chosen and files[offset:]:
            raise ArpError("ITEM_TOO_LARGE", "one file entry does not fit the page byte cap")
        return check("SkillDetailsPage", page)


__all__ = (
    "Bundle", "BundleFile", "INSTRUCTIONS_CAPABILITY_ID", "INSTRUCTIONS_SCHEMA_ID", "SkillImportResult", "SkillImporter",
    "inspect_bundle", "parse_frontmatter",
)
