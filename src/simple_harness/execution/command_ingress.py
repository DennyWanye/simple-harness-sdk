# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable command admission and lease/CAS operations on the execution DB."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from simple_harness.contracts import RunId, canonical_json
from simple_harness.runtime.commands import (
    CancelCommandIntent,
    CommandError,
    CommandErrorCode,
    CommandIntent,
    CommandKind,
    CommandOutputState,
    CommandReceipt,
    CommandRetryState,
    CommandSnapshot,
    CommandState,
    ContinueCommandIntent,
    RunApiMode,
    StartCommandIntent,
)
from simple_harness.runtime.conversation_memory import ConversationTurnOutput

if TYPE_CHECKING:
    from .sqlite.database import Database


@dataclass(frozen=True, slots=True)
class CommandClaim:
    receipt: CommandReceipt
    raw_payload_json: str
    owner_id: str
    claim_epoch: int
    lease_expires_at: float
    attempt_count: int


class CommandIngress:
    """Connection-sharing repository; it never owns a second database or worker."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def submit_start(self, intent: StartCommandIntent, *, now: float) -> CommandReceipt:
        return self._submit(intent, now=_time(now))

    def submit_continue(self, intent: ContinueCommandIntent, *, now: float) -> CommandReceipt:
        return self._submit(intent, now=_time(now))

    def submit_cancel(self, intent: CancelCommandIntent, *, now: float) -> CommandReceipt:
        return self._submit(intent, now=_time(now))

    def get(self, command_id: str) -> CommandReceipt:
        row = self._database.connection.execute(
            "SELECT * FROM conversation_commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            raise CommandError(CommandErrorCode.NOT_FOUND)
        return _receipt(row)

    def snapshot(self, command_id: str) -> CommandSnapshot:
        row = self._database.connection.execute(
            "SELECT * FROM conversation_commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            raise CommandError(CommandErrorCode.NOT_FOUND)
        receipt = _receipt(row)
        output = None
        run = self._database.connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (receipt.run_id.value,)
        ).fetchone()
        run_state = None if run is None else str(run[0])
        if run_state in {"failed", "cancelled"} or (
            run_state is None and receipt.state.terminal
        ):
            output_state = CommandOutputState.ABSENT
        elif run_state != "completed":
            output_state = CommandOutputState.PENDING
        else:
            output_state = CommandOutputState.ABSENT
            output_row = self._database.connection.execute(
                "SELECT command_id,output_json,output_hash FROM conversation_outputs "
                "WHERE run_id=?",
                (receipt.run_id.value,),
            ).fetchone()
            latest_command = self._database.connection.execute(
                "SELECT command_id FROM conversation_commands WHERE run_id=? "
                "AND state='applied' AND kind IN ('start','continue') "
                "ORDER BY accept_seq DESC LIMIT 1",
                (receipt.run_id.value,),
            ).fetchone()
            owns_terminal_output = (
                latest_command is not None
                and str(latest_command[0]) == receipt.command_id
            )
            if owns_terminal_output:
                output_state = CommandOutputState.UNKNOWN
            if owns_terminal_output and output_row is not None:
                output_json = str(output_row[1])
                valid_hash = hashlib.sha256(output_json.encode()).hexdigest()
                if (
                    str(output_row[0]) == receipt.command_id
                    and str(output_row[2]) == valid_hash
                ):
                    try:
                        raw = json.loads(output_json)
                        if isinstance(raw, dict):
                            output = ConversationTurnOutput.from_json(raw)
                            output_state = CommandOutputState.PRESENT
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        pass
        if receipt.state.terminal:
            retry = CommandRetryState.SETTLED
        elif row["owner_id"] is not None:
            retry = CommandRetryState.CLAIMED
        elif row["last_error_code"] is not None:
            retry = CommandRetryState.BACKOFF
        else:
            retry = CommandRetryState.READY
        raw_error = row["last_error_code"]
        error = None if raw_error is None else CommandErrorCode(str(raw_error))
        return CommandSnapshot(receipt, retry, output_state, output, error)

    def raw_payload(self, command_id: str) -> str | None:
        row = self._database.connection.execute(
            "SELECT raw_payload_json FROM conversation_commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise CommandError(CommandErrorCode.NOT_FOUND)
        return None if row[0] is None else str(row[0])

    def reserve_legacy_run(
        self,
        *,
        namespace: str,
        projection_key_id: str,
        run_id: str,
        intent_hash: str,
        now: float,
    ) -> None:
        now = _time(now)
        with self._database.transaction() as connection:
            _bind_namespace(connection, namespace, projection_key_id, now)
            row = connection.execute(
                "SELECT namespace,api_mode,intent_hash FROM conversation_run_modes WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO conversation_run_modes"
                    "(run_id,namespace,api_mode,intent_hash,created_at) VALUES (?,?,?,?,?)",
                    (run_id, namespace, RunApiMode.LEGACY.value, intent_hash, now),
                )
                return
            if tuple(row) != (namespace, RunApiMode.LEGACY.value, intent_hash):
                raise CommandError(CommandErrorCode.RUN_MODE_CONFLICT)

    def require_legacy_or_unmanaged(self, run_id: str) -> None:
        row = self._database.connection.execute(
            "SELECT api_mode FROM conversation_run_modes WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and str(row[0]) != RunApiMode.LEGACY.value:
            raise CommandError(CommandErrorCode.RUN_MODE_CONFLICT)

    def claim_next(self, *, owner_id: str, now: float, lease_seconds: float) -> CommandClaim | None:
        now = _time(now)
        if not owner_id.strip() or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("owner_id and positive finite lease_seconds are required")
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT c.* FROM conversation_commands c
                WHERE c.state NOT IN ('applied','rejected','cancelled')
                  AND c.retry_at <= ?
                  AND (c.owner_id IS NULL OR c.lease_expires_at <= ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM conversation_commands earlier
                    WHERE earlier.run_id=c.run_id
                      AND earlier.accept_seq<c.accept_seq
                      AND earlier.state NOT IN ('applied','rejected','cancelled')
                  )
                ORDER BY c.created_at,c.run_id,c.accept_seq LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            epoch = int(row["claim_epoch"]) + 1
            expires = now + lease_seconds
            changed = connection.execute(
                """
                UPDATE conversation_commands
                SET owner_id=?,claim_epoch=?,lease_expires_at=?,attempt_count=attempt_count+1,
                    version=version+1,updated_at=?
                WHERE command_id=? AND version=?
                  AND (owner_id IS NULL OR lease_expires_at <= ?)
                """,
                (owner_id, epoch, expires, now, row["command_id"], row["version"], now),
            ).rowcount
            if changed != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?",
                (row["command_id"],),
            ).fetchone()
            assert claimed is not None and claimed["raw_payload_json"] is not None
            return CommandClaim(
                _receipt(claimed),
                str(claimed["raw_payload_json"]),
                owner_id,
                epoch,
                expires,
                int(claimed["attempt_count"]),
            )

    def transition(
        self,
        claim: CommandClaim,
        *,
        expected: CommandState,
        target: CommandState,
        now: float,
    ) -> CommandReceipt:
        if expected.terminal or target is CommandState.ACCEPTED:
            raise ValueError("invalid command transition")
        now = _time(now)
        terminal = target.terminal
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE conversation_commands
                SET state=?,raw_payload_json=CASE WHEN ? THEN NULL ELSE raw_payload_json END,
                    owner_id=CASE WHEN ? THEN NULL ELSE owner_id END,
                    lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    version=version+1,updated_at=?
                WHERE command_id=? AND state=? AND owner_id=? AND claim_epoch=?
                """,
                (
                    target.value,
                    terminal,
                    terminal,
                    terminal,
                    now,
                    claim.receipt.command_id,
                    expected.value,
                    claim.owner_id,
                    claim.claim_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise CommandError(CommandErrorCode.INTENT_CONFLICT)
            row = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?",
                (claim.receipt.command_id,),
            ).fetchone()
            assert row is not None
            return _receipt(row)

    def retry(
        self,
        claim: CommandClaim,
        *,
        error_code: str,
        retry_at: float,
        now: float,
    ) -> CommandReceipt:
        now = _time(now)
        retry_at = _time(retry_at)
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE conversation_commands
                SET owner_id=NULL,lease_expires_at=NULL,last_error_code=?,retry_at=?,
                    version=version+1,updated_at=?
                WHERE command_id=? AND owner_id=? AND claim_epoch=?
                  AND state NOT IN ('applied','rejected','cancelled')
                """,
                (
                    error_code,
                    retry_at,
                    now,
                    claim.receipt.command_id,
                    claim.owner_id,
                    claim.claim_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise CommandError(CommandErrorCode.INTENT_CONFLICT)
            row = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?",
                (claim.receipt.command_id,),
            ).fetchone()
            assert row is not None
            return _receipt(row)

    def reject(
        self,
        claim: CommandClaim,
        *,
        error_code: CommandErrorCode,
        now: float,
    ) -> CommandReceipt:
        now = _time(now)
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE conversation_commands
                SET state='rejected',raw_payload_json=NULL,owner_id=NULL,lease_expires_at=NULL,
                    last_error_code=?,version=version+1,updated_at=?
                WHERE command_id=? AND owner_id=? AND claim_epoch=?
                  AND state NOT IN ('applied','rejected','cancelled')
                """,
                (
                    error_code.value,
                    now,
                    claim.receipt.command_id,
                    claim.owner_id,
                    claim.claim_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise CommandError(CommandErrorCode.INTENT_CONFLICT)
            row = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?",
                (claim.receipt.command_id,),
            ).fetchone()
            assert row is not None
            return _receipt(row)

    def heartbeat(self, claim: CommandClaim, *, now: float, lease_seconds: float) -> CommandClaim:
        now = _time(now)
        expires = now + lease_seconds
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE conversation_commands SET lease_expires_at=?,version=version+1,updated_at=?
                WHERE command_id=? AND owner_id=? AND claim_epoch=?
                  AND state NOT IN ('applied','rejected','cancelled')
                """,
                (expires, now, claim.receipt.command_id, claim.owner_id, claim.claim_epoch),
            ).rowcount
            if changed != 1:
                raise CommandError(CommandErrorCode.INTENT_CONFLICT)
            row = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?",
                (claim.receipt.command_id,),
            ).fetchone()
            assert row is not None
            return CommandClaim(
                _receipt(row),
                claim.raw_payload_json,
                claim.owner_id,
                claim.claim_epoch,
                expires,
                int(row["attempt_count"]),
            )

    def _submit(self, intent: CommandIntent, *, now: float) -> CommandReceipt:
        raw = canonical_json(intent.to_json())
        with self._database.transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?",
                (intent.command_id,),
            ).fetchone()
            if replay is not None:
                if (
                    replay["intent_hash"] != intent.intent_hash
                    or replay["kind"] != intent.kind.value
                    or replay["run_id"] != intent.run_id.value
                ):
                    raise CommandError(CommandErrorCode.INTENT_CONFLICT)
                return _receipt(replay)
            _bind_namespace(connection, intent.namespace, intent.projection_key_id, now)
            mode = connection.execute(
                "SELECT namespace,api_mode,intent_hash FROM conversation_run_modes WHERE run_id=?",
                (intent.run_id.value,),
            ).fetchone()
            if intent.kind is CommandKind.START:
                if mode is None:
                    connection.execute(
                        "INSERT INTO conversation_run_modes"
                        "(run_id,namespace,api_mode,intent_hash,created_at) VALUES (?,?,?,?,?)",
                        (
                            intent.run_id.value,
                            intent.namespace,
                            RunApiMode.COMMAND.value,
                            intent.intent_hash,
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO conversation_command_streams(run_id,namespace) VALUES (?,?)",
                        (intent.run_id.value, intent.namespace),
                    )
                elif tuple(mode) != (
                    intent.namespace,
                    RunApiMode.COMMAND.value,
                    intent.intent_hash,
                ):
                    raise CommandError(CommandErrorCode.RUN_MODE_CONFLICT)
                accept_seq = 0
            else:
                if mode is None:
                    raise CommandError(CommandErrorCode.NOT_FOUND)
                if tuple(mode[:2]) != (intent.namespace, RunApiMode.COMMAND.value):
                    raise CommandError(CommandErrorCode.RUN_MODE_CONFLICT)
                stream = connection.execute(
                    "SELECT next_accept_seq,cancel_fence_seq FROM conversation_command_streams "
                    "WHERE run_id=?",
                    (intent.run_id.value,),
                ).fetchone()
                assert stream is not None
                if intent.kind is CommandKind.CONTINUE and stream["cancel_fence_seq"] is not None:
                    raise CommandError(CommandErrorCode.CANCEL_FENCE)
                accept_seq = int(stream["next_accept_seq"])
                connection.execute(
                    "UPDATE conversation_command_streams SET next_accept_seq=? WHERE run_id=?",
                    (accept_seq + 1, intent.run_id.value),
                )
            connection.execute(
                """
                INSERT INTO conversation_commands(
                    command_id,namespace,projection_key_id,run_id,kind,accept_seq,intent_hash,raw_payload_json,
                    state,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,'accepted',?,?)
                """,
                (
                    intent.command_id,
                    intent.namespace,
                    intent.projection_key_id,
                    intent.run_id.value,
                    intent.kind.value,
                    accept_seq,
                    intent.intent_hash,
                    raw,
                    now,
                    now,
                ),
            )
            if intent.kind is CommandKind.CANCEL:
                connection.execute(
                    "UPDATE conversation_command_streams SET cancel_fence_seq=? WHERE run_id=?",
                    (accept_seq, intent.run_id.value),
                )
                connection.execute(
                    """
                    UPDATE conversation_commands
                    SET state='cancelled',raw_payload_json=NULL,owner_id=NULL,
                        lease_expires_at=NULL,version=version+1,updated_at=?
                    WHERE run_id=? AND accept_seq<? AND kind IN ('start','continue')
                      AND state='accepted'
                    """,
                    (now, intent.run_id.value, accept_seq),
                )
                run_exists = connection.execute(
                    "SELECT 1 FROM runs WHERE run_id=?", (intent.run_id.value,)
                ).fetchone()
                unsettled = connection.execute(
                    "SELECT 1 FROM conversation_commands WHERE run_id=? AND accept_seq<? "
                    "AND state NOT IN ('applied','rejected','cancelled') LIMIT 1",
                    (intent.run_id.value, accept_seq),
                ).fetchone()
                if run_exists is None and unsettled is None:
                    connection.execute(
                        "UPDATE conversation_commands SET state='applied',raw_payload_json=NULL,"
                        "version=version+1,updated_at=? WHERE command_id=? AND state='accepted'",
                        (now, intent.command_id),
                    )
            row = connection.execute(
                "SELECT * FROM conversation_commands WHERE command_id=?", (intent.command_id,)
            ).fetchone()
            assert row is not None
            return _receipt(row)


def _bind_namespace(
    connection: sqlite3.Connection,
    namespace: str,
    projection_key_id: str,
    now: float,
) -> None:
    row = connection.execute(
        "SELECT projection_key_id FROM conversation_command_namespaces WHERE namespace=?",
        (namespace,),
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO conversation_command_namespaces VALUES (?,?,?)",
            (namespace, projection_key_id, now),
        )
    elif row[0] != projection_key_id:
        raise CommandError(CommandErrorCode.NAMESPACE_KEY_CONFLICT)


def _receipt(row: sqlite3.Row) -> CommandReceipt:
    return CommandReceipt(
        str(row["command_id"]),
        RunId(str(row["run_id"])),
        CommandKind(str(row["kind"])),
        int(row["accept_seq"]),
        CommandState(str(row["state"])),
        int(row["version"]),
        str(row["namespace"]),
        str(row["projection_key_id"]),
        str(row["intent_hash"]),
    )


def _time(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("time must be finite")
    if value < 0:
        raise ValueError("time must be non-negative")
    return float(value)


__all__ = ("CommandClaim", "CommandIngress")
