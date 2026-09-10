# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``EmbeddingPort``: a real embedding model injected by the caller (BA24 / BA26).

The SDK ships no model.  ``fingerprint`` (model + version + dimension) keys every
stored vector and every index job, so a changed model never mixes with old rows.
Hash or word-count mocks are for logic tests only and must say so in their
fingerprint; they never count as semantic evidence (BA-v1.0 §6.3).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    @property
    def fingerprint(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Blocking; callers run it off the event loop and outside transactions."""


class EmbeddingUnavailable(RuntimeError):
    """The embedding service refused or failed; the caller must degrade visibly."""

    def __init__(self, code: str = "embedding_unavailable") -> None:
        super().__init__(code)
        self.code = code


class HashEmbedder:
    """Deterministic character-n-gram hashing embedder for logic tests.

    Its fingerprint carries ``mock`` so acceptance evidence never mistakes it
    for semantic retrieval (BA-v1.0 §6.3).
    """

    def __init__(self, dim: int = 64, *, name: str = "hash-ngram") -> None:
        self._dim = dim
        self.fingerprint = f"mock:{name}:{dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            values = [0.0] * self._dim
            grams = [text[i : i + 2] for i in range(max(1, len(text) - 1))] or [text]
            for gram in grams:
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest()
                values[int.from_bytes(digest, "big") % self._dim] += 1.0
            norm = math.sqrt(sum(v * v for v in values)) or 1.0
            vectors.append([v / norm for v in values])
        return vectors


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


__all__ = ("EmbeddingPort", "EmbeddingUnavailable", "HashEmbedder", "cosine")
