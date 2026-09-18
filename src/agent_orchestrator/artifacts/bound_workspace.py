# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3o: turn a bound InputManifest into the consumer's workspace baseline.

Hierarchical dispatch places exactly the resolved InputManifest (§24.1 decision 4):
an ORDER-only predecessor contributes nothing.  A ``patch`` port still only names
the diff document (``patch.diff``, ``out/patch.json``).  The files the producer
actually changed live on that Attempt as extra accepted artifacts — Grok C3's
apply-patch leaf accepted ``stats/window.py`` next to ``patch.diff``.

A downstream leaf (verify, inspect@2, summarize@2) that starts from the unpatched
seed then either re-applies the patch (P2.3m's same-hash rewrite) or lists the
patched path on its envelope.  ``rule_check`` looks at the Attempt's *recorded*
artifacts, which no longer include that path, and fails
``artifact '…' is not a recorded workspace file``.  ``code_test`` rebuilds from
seed + the diff document and runs against the red baseline.

This module does two pure things and writes nothing:

* :func:`overlay_bound_producer_files` — for each bound producer, add the accepted
  artifacts that overlay the consumer's seed.  The port document stays; REPORT.md
  and other non-seed files stay off the baseline (they are the producer's own
  outputs, not product code).
* :func:`bound_artifacts_named_in_envelope` — an envelope path that names a bound
  input is a recorded workspace file for ``rule_check``, even when the collector
  dropped it as "already accepted" (P2.3m).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence

from ..contracts import Artifact
from .versioning import UpstreamInput


def _is_test_artifact(path: str) -> bool:
    """P2.3t: new test files are not on the consumer seed (they are the write)."""

    return path.startswith("tests/") or path.startswith("test_")


def overlay_bound_producer_files(
    inputs: Sequence[UpstreamInput],
    *,
    seed_paths: Collection[str],
    artifacts_by_producer: Mapping[str, Sequence[Artifact]],
) -> list[UpstreamInput]:
    """The manifest entries plus each bound producer's accepted seed-path files.

    Producers the manifest did not name contribute nothing — that is the
    ORDER-only case §24.1 decision 4 already holds.  A path already in the
    manifest is left as the port document; a later producer does not override it.
    """

    occupied = {item.path: item for item in inputs}
    extra: dict[str, UpstreamInput] = {}
    seed = set(seed_paths)
    for item in inputs:
        for artifact in artifacts_by_producer.get(item.task_id, ()):
            if artifact.path in occupied or artifact.path in extra:
                continue
            if artifact.path not in seed and not _is_test_artifact(artifact.path):
                continue
            extra[artifact.path] = UpstreamInput(
                item.task_id,
                artifact.path,
                artifact.content_hash,
                artifact.id,
            )
    combined = [*inputs, *extra.values()]
    return sorted(combined, key=lambda entry: entry.path)


def bound_artifacts_named_in_envelope(
    envelope_paths: Sequence[str],
    recorded: Sequence[Artifact],
    bound: Sequence[UpstreamInput],
    lookup: Callable[[str], Artifact | None],
) -> list[Artifact]:
    """``recorded`` plus bound inputs the envelope listed but the collector dropped.

    Paths that are neither recorded nor bound stay absent — ``rule_check`` still
    reports those as unrecorded.  Lookup is the store's ``get_artifact``; a missing
    row is skipped rather than invented.
    """

    known = {artifact.path for artifact in recorded}
    extra: list[Artifact] = []
    by_path = {item.path: item for item in bound}
    for path in envelope_paths:
        if path in known:
            continue
        item = by_path.get(path)
        if item is None:
            continue
        artifact = lookup(item.artifact_id)
        if artifact is None:
            continue
        extra.append(artifact)
        known.add(path)
    return [*recorded, *extra]


__all__ = (
    "bound_artifacts_named_in_envelope",
    "overlay_bound_producer_files",
)
