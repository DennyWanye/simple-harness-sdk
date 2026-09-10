# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Real embedding bridge for opt-in tests: WeMM-Embedding-2B in the Host's venv.

The SDK venv has no torch; the Host venv (``simple_harness/backend/.venv``) has
sentence-transformers and the model under the OS user models directory.  One
subprocess per ``embed`` batch (~10 s load); tests batch their texts.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

HOST_PY = (
    Path(__file__).resolve().parents[3] / "simple_harness" / "backend" / ".venv" / "bin" / "python"
)
MODEL_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "com.dennywanye.simpleharness"
    / "models"
    / "wemm-embedding-2b"
)

_SCRIPT = r"""
import json, sys, os
os.environ["HF_HUB_OFFLINE"] = "1"; os.environ["TRANSFORMERS_OFFLINE"] = "1"
from sentence_transformers import SentenceTransformer
model = SentenceTransformer(sys.argv[1], device="cpu", trust_remote_code=True)
texts = json.load(sys.stdin)
vectors = model.encode(texts, normalize_embeddings=True)
json.dump([[float(x) for x in row] for row in vectors], sys.stdout)
"""


def available() -> bool:
    return HOST_PY.is_file() and MODEL_DIR.is_dir()


class HostVenvWemmEmbedder:
    fingerprint = "wemm-embedding-2b:host-venv:v1"

    def __init__(self) -> None:
        self._dim: int | None = None
        self.calls = 0

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["probe"])[0])
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        completed = subprocess.run(
            [str(HOST_PY), "-c", _SCRIPT, str(MODEL_DIR)],
            input=json.dumps(list(texts), ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "TOKENIZERS_PARALLELISM": "false"},
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"embedding subprocess failed: {completed.stderr[-500:]}")
        vectors = json.loads(completed.stdout)
        self._dim = len(vectors[0]) if vectors else self._dim
        return vectors
