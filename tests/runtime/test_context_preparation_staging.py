# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from pathlib import Path

from simple_harness.execution.context_staging import (
    ContextStageKind,
    ContextStageState,
    ContextStagingRepository,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork


def test_expired_owner_is_taken_over_and_consumed_bytes_are_cleared(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        repository = ContextStagingRepository(database)
        input_hash = hashlib.sha256(b"input").hexdigest()
        first = repository.claim(
            stage_id="stage-1",
            kind=ContextStageKind.ROOT,
            identity_key="request-1",
            user_id="user-1",
            session_id="session-1",
            input_hash=input_hash,
            mode="consumer_prepared",
            owner_id="owner-1",
            now=1.0,
            lease_seconds=5.0,
        )
        assert first.owner
        assert not repository.claim(
            stage_id="stage-1",
            kind=ContextStageKind.ROOT,
            identity_key="request-1",
            user_id="user-1",
            session_id="session-1",
            input_hash=input_hash,
            mode="consumer_prepared",
            owner_id="owner-2",
            now=2.0,
            lease_seconds=5.0,
        ).owner
        takeover = repository.claim(
            stage_id="stage-1",
            kind=ContextStageKind.ROOT,
            identity_key="request-1",
            user_id="user-1",
            session_id="session-1",
            input_hash=input_hash,
            mode="consumer_prepared",
            owner_id="owner-2",
            now=6.0,
            lease_seconds=5.0,
        )
        assert takeover.owner
        staged = repository.complete(
            takeover.record,
            private_snapshot={"provider_messages": []},
            memory_result_id=None,
            memory_result_hash=None,
            now=7.0,
        )
        assert staged.private_snapshot_hash is not None
        uow = SqliteExecutionUnitOfWork(database)
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={"prepared_context": {"provider_messages": []}},
            event_id="run-1:created",
            user_id="user-1",
            context_stage_id=staged.stage_id,
            context_stage_hash=staged.private_snapshot_hash,
            now=8.0,
        )
        consumed = repository.get("stage-1")
        assert consumed is not None and consumed.state is ContextStageState.CONSUMED
        assert consumed.private_snapshot is None
        assert consumed.private_snapshot_hash == staged.private_snapshot_hash


def test_cleanup_retains_active_and_recent_staged_rows(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        repository = ContextStagingRepository(database)
        digest = hashlib.sha256(b"input").hexdigest()
        repository.claim(
            stage_id="active",
            kind=ContextStageKind.ROOT,
            identity_key="active-request",
            user_id="user-1",
            session_id="session-1",
            input_hash=digest,
            mode="sdk_prepared",
            owner_id="owner-1",
            now=5.0,
            lease_seconds=100.0,
        )
        assert repository.cleanup(now=10.0, older_than=1.0, limit=10) == 0
        assert repository.get("active") is not None
