# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static structural-conformance fixture for Host main-model analysis executors."""

from simple_harness.runtime.evidence_protocol import (
    MemoryAnalysisExecutorPort,
    MemoryAnalysisRequest,
    MemoryAnalysisResult,
)


class StructuralMemoryAnalysisExecutor:
    async def analyze_memory(self, request: MemoryAnalysisRequest) -> MemoryAnalysisResult:
        raise NotImplementedError(request)


def accepts_executor(value: MemoryAnalysisExecutorPort) -> MemoryAnalysisExecutorPort:
    return value


STRUCTURAL_EXECUTOR: MemoryAnalysisExecutorPort = accepts_executor(
    StructuralMemoryAnalysisExecutor()
)
