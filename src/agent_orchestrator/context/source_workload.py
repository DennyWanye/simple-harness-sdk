# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Frozen source-size facts for planning; no source prose becomes an instruction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import Mission
from ..governance.domains import DomainProfileV1
from ..runtime.model_router import RuntimeProfile
from ..verification.evidence_resolver import EvidenceResolver


def source_workload(
    *,
    store: Any,
    artifacts: Any,
    mission: Mission,
    domain: DomainProfileV1,
    versions: Mapping[str, str],
    profiles: Mapping[str, RuntimeProfile],
    config: Any,
) -> dict[str, Any]:
    resolver = EvidenceResolver(store, artifacts)
    sources: list[dict[str, Any]] = []
    counts: dict[str, int] = {name: 0 for name in profiles}
    for path, version in sorted(versions.items()):
        source = resolver.read_source(
            tenant_id=mission.tenant_id,
            mission_id=mission.id,
            path=path,
            version=version,
            source_roots=domain.source_roots,
        )
        if source.status != "resolved" or source.data is None:
            raise ValueError(f"planning source is unavailable: {source.status}")
        text = source.data.decode("utf-8")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        sources.append(
            {
                "path": path,
                "version": version,
                "bytes": len(source.data),
                "unicode_chars": len(text),
                "lines": 0
                if not normalized
                else len(normalized.split("\n")) - int(normalized.endswith("\n")),
            }
        )
        for name, profile in profiles.items():
            if profile.tokenizer is not None:
                counts[name] += profile.tokenizer.count_text(text)
    return {
        "schema": "source-workload-v1",
        "sources": sources,
        "total_bytes": sum(item["bytes"] for item in sources),
        "total_unicode_chars": sum(item["unicode_chars"] for item in sources),
        "profiles": [
            {
                "profile_id": name,
                "model": profile.model,
                "tokenizer_fingerprint": None
                if profile.tokenizer is None
                else profile.tokenizer.fingerprint,
                "source_text_tokens": None if profile.tokenizer is None else counts[name],
                "default_output_cap": profile.default_max_output_tokens
                or config.default_max_output_tokens,
                "maximum_output_cap": profile.max_output_tokens_ceiling
                or config.max_output_tokens_ceiling,
            }
            for name, profile in sorted(profiles.items())
        ],
        "scope": (
            "source text only; excludes prompts, tool schemas, history, outputs "
            "and independent verification"
        ),
        "note": (
            "来源个数不是Task数量；每个Task都要支付多轮输入、输出及独立Critic成本。"
            "规模统计不是完整费用估算。"
        ),
    }
