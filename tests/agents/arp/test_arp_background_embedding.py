# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""A heavy (real) embedding model never runs inside a Turn (2026-09-24).

With a port that declares ``prefers_background`` the prepare path no longer processes
INDEX jobs inline; the background pump claims them, computes the vectors in a worker
thread (pure computation, no database), then processes each job on the loop as before —
its embedding call is still recorded intent-first and SUCCEEDED, served from the
precomputed vectors.  Recall of an early fact keeps working (hybrid).
"""

from __future__ import annotations

import asyncio
import threading

from arp_fixture import build
from provider_fixture import ScriptedProvider
from test_arp_index_recall import FILLERS, SMALL, _latest_manifest, _recall_of, _seed, _turn

from simple_harness.agents.arp import store
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.agents.memory.embedding import HashEmbedder


class HeavyEmbedder(HashEmbedder):
    prefers_background = True

    def __init__(self) -> None:
        super().__init__(dim=32, name="heavy-hash")
        self.calls: list[tuple[str, int]] = []

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.calls.append((threading.current_thread().name, len(texts)))
        return super().embed(texts)


def test_index_embeddings_run_off_the_loop_and_recall_still_works(tmp_path) -> None:
    async def case() -> None:
        embedder = HeavyEmbedder()
        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS) + ["暗号是蓝鲸七号。"])
        runtime = build(tmp_path, provider, embedding=embedder, **SMALL)
        async with runtime:
            assert runtime.arp.index.background_embedding is True
            agent = await _seed(runtime, len(FILLERS) + 2)
            connection = runtime.uow.database.connection
            for _ in range(200):  # the pump drains the INDEX jobs between Turns
                pending = connection.execute("SELECT COUNT(*) FROM arp_jobs WHERE kind='INDEX' AND state!='DONE'").fetchone()[0]
                if pending == 0:
                    break
                await asyncio.sleep(0.05)
            assert pending == 0
            done = connection.execute("SELECT COUNT(*) FROM arp_jobs WHERE kind='INDEX' AND state='DONE'").fetchone()[0]
            assert done >= 2 * len(FILLERS)
            index_calls = connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE kind='arp.embedding_call.v1' AND payload_json LIKE '%SESSION_INDEX%' AND payload_json LIKE '%SUCCEEDED%'"
            ).fetchone()[0]
            assert index_calls >= done  # every job's embedding call is still recorded
            main = threading.main_thread().name
            batch_calls = [c for c in embedder.calls if c[1] > 1 or c[0] != main]
            assert batch_calls and all(name != main for name, _ in batch_calls)  # index vectors: worker thread
            session = store.read_live_session(connection, agent.agent_id)
            assert store.active_index_publication(connection, session.session_id) is not None
            result = await _turn(agent, "请问工程暗号 是什么？", "ask")
            assert result.state is AgentTurnState.COMMITTED
            manifest = _latest_manifest(runtime)
            recall = _recall_of(runtime, manifest)
            assert recall.phase == "READY" and recall.result["coverage"]["mode"] == "HYBRID"
            assert manifest.manifest["recalled_chunk_ids"], "the early secret still comes back through recall"

    asyncio.run(case())


def test_a_light_embedding_port_keeps_the_inline_path() -> None:
    from types import SimpleNamespace

    from simple_harness.agents.arp.indexing import SessionIndexCoordinator

    light = SessionIndexCoordinator.background_embedding.fget(SimpleNamespace(embedding=HashEmbedder(dim=8)))  # type: ignore[union-attr]
    none = SessionIndexCoordinator.background_embedding.fget(SimpleNamespace(embedding=None))  # type: ignore[union-attr]
    assert light is False and none is False
