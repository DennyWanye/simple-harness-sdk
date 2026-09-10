# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Lexical query shaping for the derived FTS5 tables (BA24, [W02]).

* Identifiers, paths and function names (``auth.py``, ``refresh_token``,
  ``src/foo/bar``) go to the *words* index with an exact-token query.
* Everything else, including Chinese, goes to the *trigram* index: a
  character-trigram query needs no word boundaries and works for CJK.
* Queries shorter than three characters cannot use trigram; they fall back to a
  substring scan over the Journal (the "short word" path of [W02]).
"""

from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./-][A-Za-z0-9_]+)+|[A-Za-z_][A-Za-z0-9_]{2,}")
_FTS_SPECIAL = re.compile(r'["*()^:\-+]')


def identifiers(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0) for match in _IDENTIFIER.finditer(query)))


def words_match(tokens: tuple[str, ...]) -> str | None:
    if not tokens:
        return None
    quoted = [f'"{_FTS_SPECIAL.sub(" ", token).strip()}"' for token in tokens]
    return " OR ".join(q for q in quoted if q != '""')


def trigram_match(query: str, *, max_windows: int = 24) -> str | None:
    """OR of the query's character trigrams: Chinese needs no word boundaries and a
    paraphrase that shares only some windows still ranks (bm25 counts matches)."""

    text = _FTS_SPECIAL.sub(" ", query).strip()
    if len(text) < 3:
        return None
    windows: list[str] = []
    for chunk in text.split():
        if len(chunk) < 3:
            continue
        windows.extend(chunk[i : i + 3] for i in range(len(chunk) - 2))
    unique = list(dict.fromkeys(windows))[:max_windows]
    if not unique:
        return None
    return " OR ".join(f'"{w}"' for w in unique)


def short_terms(query: str) -> tuple[str, ...]:
    """Terms too short for trigram (1–2 characters) that still deserve an exact scan."""

    parts = [p for p in re.split(r"\s+", query.strip()) if p]
    return tuple(p for p in parts if 0 < len(p) < 3)


__all__ = ("identifiers", "short_terms", "trigram_match", "words_match")
