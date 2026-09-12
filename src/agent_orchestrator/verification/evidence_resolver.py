# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic source attribution, not verification of a source's assertions.

Read only exact registered versions from CAS. Mutable lifecycle flags are deliberately
excluded: acceptance checks them separately; resolution uses an Attempt's frozen map.
Source text, including apparent instructions, is always data.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..artifacts.store import ArtifactStore, ArtifactStoreError
from ..contracts import SourceCitation

DISPLAY_PREVIEW_CHARS = 2048
HEADING_PREVIEW_CHARS = 256
EVIDENCE_RESOLUTION_SCHEMA_VERSION = 1
ResolutionStatus = Literal[
    "resolved",
    "not_found",
    "unreadable",
    "stale_source",
    "span_out_of_range",
    "quote_mismatch",
    "quote_ambiguous",
    "quote_not_whole_unit",
]
ReadStatus = Literal["resolved", "not_found", "unreadable"]


class SourceStore(Protocol):
    def get_source(
        self, mission_id: str, path: str, version_hash: str | None = None
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class SourceRead:
    """Authorized CAS bytes plus decoded text; no mutable registry flags escape.

    Failures never carry bytes/text. ``not_found`` also hides every metadata field.
    ``reason`` is intentionally coarse and stable; storage diagnostics are not exposed.
    """

    status: ReadStatus
    data: bytes | None = None
    text: str | None = None
    path: str | None = None
    version_hash: str | None = None
    kind: str | None = None
    trust: str | None = None
    tenant_id: str | None = None
    mission_id: str | None = None

    @property
    def reason(self) -> str | None:
        return None if self.status == "resolved" else self.status


@dataclass(frozen=True, slots=True)
class Locator:
    start_line: int
    end_line: int

    def to_json(self) -> dict[str, int]:
        return {"start_line": self.start_line, "end_line": self.end_line}


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    start_line: int
    end_line: int
    preview: str

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class DisplayBlock:
    start_line: int
    end_line: int
    headings: tuple[Heading, ...]
    preview: str
    truncated: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "headings": [heading.to_json() for heading in self.headings],
            "preview": self.preview,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class EvidenceResolutionV1:
    status: ResolutionStatus
    ref: SourceCitation | None = None
    kind: str | None = None
    target: str | None = None
    source_version: str | None = None
    locator: Locator | None = None
    display_block: DisplayBlock | None = None
    tenant_id: str | None = None
    mission_id: str | None = None
    source_trust: str | None = None

    @property
    def factual_status(self) -> bool:
        """True means attributable to these bytes, never VERIFIED world knowledge."""
        return self.status == "resolved"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_RESOLUTION_SCHEMA_VERSION,
            "ref": self.ref.to_json() if self.ref is not None else None,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "source_version": self.source_version,
            "locator": self.locator.to_json() if self.locator is not None else None,
            "display_block": self.display_block.to_json() if self.display_block else None,
            "tenant_id": self.tenant_id,
            "mission_id": self.mission_id,
            "source_trust": self.source_trust,
        }


def _safe_path(value: str) -> bool:
    # Registry names are workspace-relative POSIX paths. Reject aliases rather than
    # accidentally widening a root or changing the key used for exact-version lookup.
    return bool(value) and not (
        value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    )


class EvidenceResolver:
    def __init__(self, store: SourceStore, artifact_store: ArtifactStore) -> None:
        self.store = store
        self.artifact_store = artifact_store

    def read_source(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        path: str,
        version: str,
        source_roots: Sequence[str],
    ) -> SourceRead:
        """The shared exact-version authority/CAS entrance for materializers/adapters.

        Pass versions from the frozen map when materializing. No filesystem path or
        storage_uri supplied by a source is followed. Database faults remain faults.
        """
        roots = tuple(root.rstrip("/") for root in source_roots)
        if not _safe_path(path) or not any(
            _safe_path(root) and (path == root or path.startswith(root + "/")) for root in roots
        ):
            return SourceRead("not_found")
        row = self.store.get_source(mission_id, path, version)
        if row is None or any(
            row.get(key) != value
            for key, value in (
                ("tenant_id", tenant_id),
                ("mission_id", mission_id),
                ("path", path),
                ("version_hash", version),
            )
        ):
            return SourceRead("not_found")
        metadata = {
            "path": path,
            "version_hash": version,
            "kind": row["kind"],
            "trust": row["trust"],
            "tenant_id": tenant_id,
            "mission_id": mission_id,
        }
        try:
            data = self.artifact_store.read(version)
            text = data.decode("utf-8", errors="strict")
        except (ArtifactStoreError, OSError, UnicodeDecodeError):
            return SourceRead("unreadable", **metadata)
        return SourceRead("resolved", data=data, text=text, **metadata)

    def resolve(
        self,
        citation: SourceCitation,
        *,
        tenant_id: str,
        mission_id: str,
        source_versions: Mapping[str, str],
        source_roots: Sequence[str],
    ) -> EvidenceResolutionV1:
        read = self.read_source(
            tenant_id=tenant_id,
            mission_id=mission_id,
            path=citation.path,
            version=citation.version,
            source_roots=source_roots,
        )
        if read.status == "not_found":
            # No query, identity, root, discovered row, or locator in this response.
            return EvidenceResolutionV1("not_found")

        def result(
            status: ResolutionStatus,
            locator: Locator | None = None,
            display: DisplayBlock | None = None,
        ) -> EvidenceResolutionV1:
            return EvidenceResolutionV1(
                status,
                citation,
                "source",
                read.path,
                read.version_hash,
                locator,
                display,
                read.tenant_id,
                read.mission_id,
                read.trust,
            )

        if read.status != "resolved":
            return result(read.status)
        if source_versions.get(citation.path) != citation.version:
            return result("stale_source")
        assert read.text is not None
        document = _Document(
            read.text,
            csv_source=citation.path.lower().endswith(".csv") or read.kind in {"csv", "text/csv"},
        )
        if not 1 <= citation.start_line <= citation.end_line <= len(document.lines):
            return result("span_out_of_range")
        normalized, offsets = _fold(document.text)
        quote, _ = _fold(unicodedata.normalize("NFC", citation.quote))
        first = normalized.find(quote) if quote else -1
        if first < 0:
            return result("quote_mismatch")
        if normalized.find(quote, first + 1) >= 0:
            return result("quote_ambiguous")
        start, end = offsets[first][0], offsets[first + len(quote) - 1][1]
        if not document.whole_unit(start, end):
            return result("quote_not_whole_unit")
        locator = Locator(document.line_at(start), document.line_at(end - 1))
        if locator.start_line < citation.start_line or locator.end_line > citation.end_line:
            return result("quote_mismatch")
        return result("resolved", locator, document.display(locator))


def _fold(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Whitespace fold with an index back into NFC text (never per-character NFC)."""
    pieces: list[str] = []
    offsets: list[tuple[int, int]] = []
    for match in re.finditer(r"\S+", text):
        if offsets:
            pieces.append(" ")
            offsets.append((offsets[-1][1], match.start()))
        pieces.append(match.group())
        offsets.extend((i, i + 1) for i in range(match.start(), match.end()))
    return "".join(pieces), offsets


_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_LIST = re.compile(r"^( *)(?:[-+*]|\d+[.)])[ \t]+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SENTENCE_END = re.compile(r"[。！？]+[\"'”’」』）)》】]*")
_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}


def _table_separator(line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


@dataclass(frozen=True, slots=True)
class _Block:
    start: int  # zero-based line indices, end exclusive
    end: int
    headings: tuple[Heading, ...]


class _Document:
    """Small conservative block grammar; unit anchors precede whitespace folding.

    Soft line wraps are not sentence boundaries. Lists include lazy/indented
    continuations; nested items cannot be detached from their enclosing item. Tables
    permit rows only, never sentence-like fragments inside cells. Fenced code is opaque.
    """

    def __init__(self, text: str, *, csv_source: bool = False) -> None:
        physical = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = physical.split("\n")
        if self.lines[-1] == "":
            self.lines.pop()
        self.text = unicodedata.normalize("NFC", "\n".join(self.lines))
        self.normal_lines = self.text.split("\n") if self.lines else []
        self.line_starts: list[int] = []
        offset = 0
        for line in self.normal_lines:
            self.line_starts.append(offset)
            offset += len(line) + 1
        self.blocks: list[_Block] = []
        # Each group admits a consecutive run of full sentences, or the entire unit.
        self.units: list[tuple[set[int], set[int]]] = []
        if csv_source:
            self._parse_csv()
        else:
            self._parse()

    def _parse_csv(self) -> None:
        if not self.lines:
            return
        self.blocks.append(_Block(0, len(self.lines), ()))
        reader = csv.reader(io.StringIO(self.text, newline=""), strict=True)
        start = 0
        try:
            for _ in reader:
                self._unit(start, reader.line_num)
                start = reader.line_num
        except csv.Error:
            # A malformed table has no trustworthy row boundaries. It can still be
            # quoted in full, but cannot be sliced at punctuation inside a cell.
            self.units.clear()
            self._unit(0, len(self.lines))

    def line_at(self, offset: int) -> int:
        return bisect_right(self.line_starts, offset)

    def _unit(self, first: int, last: int, *, sentences: bool = False) -> None:
        start = self.line_starts[first]
        end = self.line_starts[last - 1] + len(self.normal_lines[last - 1])
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        self.units.append(({start}, {end}))
        if not sentences:
            return
        starts, ends = {start}, set()
        closers: list[str] = []
        scanned = start
        for match in _SENTENCE_END.finditer(self.text, start, end):
            # A quoted sentence end belongs to its enclosing sentence. Scan through
            # earlier matches too, so their closing quotes restore the outer level.
            for index in range(scanned, match.start()):
                char = self.text[index]
                if (
                    char == "'"
                    and index > start
                    and index + 1 < end
                    and self.text[index - 1].isalnum()
                    and self.text[index + 1].isalnum()
                ):
                    continue  # an apostrophe within a word is not a quotation
                if closers and char == closers[-1]:
                    closers.pop()
                elif char in _QUOTE_PAIRS:
                    closers.append(_QUOTE_PAIRS[char])
            scanned = match.start()
            if closers:
                continue
            ends.add(match.end())
            following = match.end()
            while following < end and self.text[following].isspace():
                following += 1
            if following < end:
                starts.add(following)
        self.units.append((starts, ends))

    def _list_unit(self, first: int, last: int, *, nested: bool) -> None:
        self._unit(first, last)
        # Even a punctuated sentence cannot detach an item's qualifying continuation.
        # The marker itself may be omitted only when the entire leaf item's body is
        # quoted. Nested items stay attached to their enclosing item's premise.
        marker = _LIST.match(self.normal_lines[first])
        if marker and not nested:
            self.units.append(
                (
                    {self.line_starts[first] + marker.end()},
                    self.units[-1][1],
                )
            )

    def _heading(self, index: int) -> tuple[int, int] | None:
        if match := _HEADING.match(self.normal_lines[index]):
            return len(match[1]), index + 1
        if index + 1 < len(self.lines) and (match := _SETEXT.match(self.normal_lines[index + 1])):
            return (1 if match[1][0] == "=" else 2), index + 2
        return None

    def _table(self, index: int) -> bool:
        return (
            index + 1 < len(self.lines)
            and not _HEADING.match(self.normal_lines[index])
            and "|" in self.normal_lines[index]
            and _table_separator(self.normal_lines[index + 1])
        )

    def _parse(self) -> None:
        headings: list[Heading] = []
        n, i = len(self.lines), 0
        while i < n:
            line = self.normal_lines[i]
            if not line.strip():
                i += 1
                continue
            first = i
            parents = tuple(headings)
            if fence := _FENCE.match(line):
                i += 1
                closing = re.compile(
                    r"^ {0,3}" + re.escape(fence[1][0]) + "{" + str(len(fence[1])) + r",}[ \t]*$"
                )
                while i < n:
                    i += 1
                    if closing.fullmatch(self.normal_lines[i - 1]):
                        break
                self._unit(first, i)
            elif self._table(i):
                i += 2
                while i < n and "|" in self.normal_lines[i] and self.normal_lines[i].strip():
                    if _HEADING.match(self.normal_lines[i]):
                        break  # a pipe in a heading does not turn it into a table row
                    i += 1
                for row in range(first, i):
                    self._unit(row, row + 1)
            elif heading := self._heading(i):
                level, i = heading
                while headings and headings[-1].level >= level:
                    headings.pop()
                parents = tuple(headings)
                headings.append(
                    Heading(
                        level,
                        first + 1,
                        i,
                        "\n".join(self.lines[first:i])[:HEADING_PREVIEW_CHARS],
                    )
                )
                self._unit(first, i)
            elif item := _LIST.match(line):
                indent = len(item[1])
                content_indent = item.end()
                item_start = i
                i += 1
                nested = False
                while i < n:
                    current = self.normal_lines[i]
                    if not current.strip():
                        # Keep blank lines only when an indented continuation or
                        # another item follows; unrelated following prose ends a list.
                        next_line = i + 1
                        while next_line < n and not self.normal_lines[next_line].strip():
                            next_line += 1
                        if next_line == n or not (
                            _LIST.match(self.normal_lines[next_line])
                            or len(self.normal_lines[next_line])
                            - len(self.normal_lines[next_line].lstrip())
                            > indent
                        ):
                            break
                        i = next_line
                        current = self.normal_lines[i]
                    bullet = _LIST.match(current)
                    if bullet and len(bullet[1]) <= indent:
                        self._list_unit(item_start, i, nested=nested)
                        item_start, nested = i, False
                        content_indent = bullet.end()
                    elif bullet:
                        nested = True
                    elif _HEADING.match(current) or _FENCE.match(current) or self._table(i):
                        if len(current) - len(current.lstrip(" ")) < content_indent:
                            break
                        # Indented blocks stay in the item's complete unit; otherwise
                        # its first sentence could lose a heading and its conditions.
                    i += 1
                self._list_unit(item_start, i, nested=nested)
            else:
                i += 1
                while i < n and self.normal_lines[i].strip():
                    if (
                        _LIST.match(self.normal_lines[i])
                        or _FENCE.match(self.normal_lines[i])
                        or self._heading(i)
                        or self._table(i)
                    ):
                        break
                    i += 1
                self._unit(first, i, sentences=True)
            self.blocks.append(_Block(first, i, parents))

    def whole_unit(self, start: int, end: int) -> bool:
        return any(start in starts and end in ends for starts, ends in self.units)

    def display(self, locator: Locator) -> DisplayBlock:
        blocks = [
            block
            for block in self.blocks
            if block.start < locator.end_line and block.end >= locator.start_line
        ]
        first, last = blocks[0], blocks[-1]
        text = "\n".join(self.lines[first.start : last.end])
        return DisplayBlock(
            first.start + 1,
            last.end,
            first.headings,
            text[:DISPLAY_PREVIEW_CHARS],
            len(text) > DISPLAY_PREVIEW_CHARS,
        )


__all__ = (
    "DISPLAY_PREVIEW_CHARS",
    "EVIDENCE_RESOLUTION_SCHEMA_VERSION",
    "DisplayBlock",
    "EvidenceResolutionV1",
    "EvidenceResolver",
    "Heading",
    "Locator",
    "SourceRead",
)
