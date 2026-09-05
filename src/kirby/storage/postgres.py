"""Explicit SQL for the first release: no retries after an interrupted turn."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kirby.contracts import TERMINAL_STATES, Conflict, NotFound, Principal, TaskRecord

TASK_SELECT = """
    SELECT t.id, t.context_id, t.role_id, t.tenant, t.subject, t.message_id,
           t.input_text, t.state, c.manifest, t.created_at, t.updated_at,
           t.result, t.error, t.cancel_requested, t.worker_id,
           t.session_id, t.session_path, t.attachments, t.output_files
    FROM tasks t JOIN contexts c ON c.id = t.context_id
"""


def _scope(principal: Principal, role_id: str) -> tuple[str, str, str]:
    if role_id not in principal.roles:
        raise NotFound("Role not available")
    return principal.tenant, principal.subject, role_id


class Store:
    def __init__(self, dsn: str):
        self.pool = AsyncConnectionPool(
            dsn, min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row}
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)

    async def close(self) -> None:
        await self.pool.close()

    async def migrate(self) -> None:
        async with self.pool.connection() as conn:
            for migration in sorted(Path(__file__).parent.glob("*.sql")):
                await conn.execute(migration.read_text())

    async def health(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT attachments, output_files FROM tasks LIMIT 0")
            await conn.execute("SELECT 1 FROM files LIMIT 0")
        return True

    async def create_file(
        self,
        principal: Principal,
        role_id: str,
        file_id: str,
        filename: str,
        media_type: str,
        size: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= 67108864
        ):
            raise ValueError("File size must be between 0 and 64 MiB")
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO files (id, tenant, subject, role_id, filename, "
                "media_type, size) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, filename, media_type, size, status, object_key, etag",
                (file_id, *_scope(principal, role_id), filename, media_type, size),
            )
            return await cursor.fetchone()

    async def get_file(
        self, principal: Principal, role_id: str, file_id: str
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT id, filename, media_type, size, status, object_key, etag "
                "FROM files WHERE id = %s AND tenant = %s AND subject = %s "
                "AND role_id = %s",
                (file_id, *_scope(principal, role_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFound("File not found")
            return row

    async def complete_file(
        self,
        principal: Principal,
        role_id: str,
        file_id: str,
        object_key: str,
        etag: str,
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT id, filename, media_type, size, status, object_key, etag "
                "FROM files WHERE id = %s AND tenant = %s AND subject = %s "
                "AND role_id = %s FOR UPDATE",
                (file_id, *_scope(principal, role_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFound("File not found")
            if row["status"] == "ready":
                if row["object_key"] != object_key or row["etag"] != etag:
                    raise Conflict("File already completed with different contents")
                return row
            cursor = await conn.execute(
                "UPDATE files SET status = 'ready', object_key = %s, etag = %s "
                "WHERE id = %s RETURNING id, filename, media_type, size, status, "
                "object_key, etag",
                (object_key, etag, file_id),
            )
            return await cursor.fetchone()

    async def submit(
        self,
        principal: Principal,
        role_id: str,
        message_id: str,
        text: str,
        manifest: dict[str, Any],
        context_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> TaskRecord:
        scope = _scope(principal, role_id)
        requested_context_id = context_id
        requested_attachments = attachments or []
        async with self.pool.connection() as conn:
            # Serialize the same message across API replicas before creating a context.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (json.dumps([*scope, message_id]),),
            )
            cursor = await conn.execute(
                TASK_SELECT + "WHERE t.tenant = %s AND t.subject = %s "
                "AND t.role_id = %s AND t.message_id = %s",
                (*scope, message_id),
            )
            existing = await cursor.fetchone()
            if existing:
                cursor = await conn.execute(
                    "SELECT requested_context_id, requested_attachments "
                    "FROM tasks WHERE id = %s",
                    (existing["id"],),
                )
                request = await cursor.fetchone()
                if (
                    existing["input_text"] != text
                    or request["requested_context_id"] != requested_context_id
                    or request["requested_attachments"] != requested_attachments
                ):
                    raise Conflict("Message ID already used for a different request")
                return TaskRecord(**existing)

            if context_id:
                cursor = await conn.execute(
                    "SELECT * FROM contexts WHERE id = %s AND tenant = %s "
                    "AND subject = %s AND role_id = %s FOR UPDATE",
                    (context_id, *scope),
                )
                context = await cursor.fetchone()
                if context is None:
                    raise NotFound("Context not found")
                if not context["usable"]:
                    raise Conflict("Context interrupted; create a new context")
                cursor = await conn.execute(
                    "SELECT id FROM tasks WHERE context_id = %s "
                    "AND state IN ('queued', 'running')",
                    (context_id,),
                )
                if await cursor.fetchone():
                    raise Conflict("Context already has an active task")
            else:
                context_id = str(uuid4())
                cursor = await conn.execute(
                    "INSERT INTO contexts (id, tenant, subject, role_id, manifest) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                    (context_id, *scope, Jsonb(manifest)),
                )
                context = await cursor.fetchone()

            # Resolve every new reference against the authenticated durable file row.
            merged_attachments = {item["id"]: item for item in context["attachments"]}
            for attachment in requested_attachments:
                cursor = await conn.execute(
                    "SELECT id, filename, media_type, size, object_key, etag "
                    "FROM files WHERE id = %s AND tenant = %s AND subject = %s "
                    "AND role_id = %s AND status = 'ready'",
                    (attachment["id"], *scope),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise NotFound("Ready file not found")
                merged_attachments[row["id"]] = row
            if len(merged_attachments) > 4:
                raise Conflict("A context accepts at most four files")
            effective_attachments = list(merged_attachments.values())
            await conn.execute(
                "UPDATE contexts SET attachments = %s WHERE id = %s",
                (Jsonb(effective_attachments), context_id),
            )

            task_id = str(uuid4())
            await conn.execute(
                "INSERT INTO tasks (id, context_id, tenant, subject, role_id, "
                "message_id, requested_context_id, input_text, state, "
                "session_id, session_path, requested_attachments, attachments) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s)",
                (
                    task_id,
                    context_id,
                    *scope,
                    message_id,
                    requested_context_id,
                    text,
                    context["session_id"],
                    context["session_path"],
                    Jsonb(requested_attachments),
                    Jsonb(effective_attachments),
                ),
            )
            cursor = await conn.execute(TASK_SELECT + "WHERE t.id = %s", (task_id,))
            return TaskRecord(**await cursor.fetchone())

    async def get(self, principal: Principal, role_id: str, task_id: str) -> TaskRecord:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                TASK_SELECT + "WHERE t.id = %s AND t.tenant = %s "
                "AND t.subject = %s AND t.role_id = %s",
                (task_id, *_scope(principal, role_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFound("Task not found")
            return TaskRecord(**row)

    async def list(
        self,
        principal: Principal,
        role_id: str,
        context_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskRecord]:
        if not 1 <= limit <= 101 or offset < 0:
            raise ValueError("Limit must be 1..101 and offset must be nonnegative")
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                TASK_SELECT + "WHERE t.tenant = %s AND t.subject = %s "
                "AND t.role_id = %s AND (%s::text IS NULL OR t.context_id = %s) "
                "ORDER BY t.created_at DESC, t.id LIMIT %s OFFSET %s",
                (*_scope(principal, role_id), context_id, context_id, limit, offset),
            )
            return [TaskRecord(**row) for row in await cursor.fetchall()]

    async def cancel(
        self, principal: Principal, role_id: str, task_id: str
    ) -> TaskRecord:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                TASK_SELECT + "WHERE t.id = %s AND t.tenant = %s "
                "AND t.subject = %s AND t.role_id = %s FOR UPDATE OF t",
                (task_id, *_scope(principal, role_id)),
            )
            task = await cursor.fetchone()
            if task is None:
                raise NotFound("Task not found")
            if task["state"] in TERMINAL_STATES:
                return TaskRecord(**task)
            state = "canceled" if task["state"] == "queued" else "running"
            await conn.execute(
                "UPDATE tasks SET cancel_requested = true, state = %s, "
                "updated_at = clock_timestamp() WHERE id = %s",
                (state, task_id),
            )
            if state == "canceled":
                await conn.execute(
                    "UPDATE contexts SET usable = false WHERE id = %s",
                    (task["context_id"],),
                )
            cursor = await conn.execute(TASK_SELECT + "WHERE t.id = %s", (task_id,))
            return TaskRecord(**await cursor.fetchone())

    async def claim(self, worker_id: str, lease_seconds: int = 30) -> TaskRecord | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE tasks SET state = 'running', worker_id = %s, "
                "lease_expires_at = clock_timestamp() + %s * interval '1 second', "
                "updated_at = clock_timestamp() WHERE id = "
                "(SELECT id FROM tasks WHERE state = 'queued' "
                "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id",
                (worker_id, lease_seconds),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await conn.execute(TASK_SELECT + "WHERE t.id = %s", (row["id"],))
            return TaskRecord(**await cursor.fetchone())

    async def heartbeat(
        self, task_id: str, worker_id: str, lease_seconds: int = 30
    ) -> bool:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE tasks SET lease_expires_at = "
                "clock_timestamp() + %s * interval '1 second' "
                "WHERE id = %s AND worker_id = %s AND state = 'running' "
                "AND lease_expires_at > clock_timestamp()",
                (lease_seconds, task_id, worker_id),
            )
            return cursor.rowcount == 1

    async def control(self, task_id: str, worker_id: str) -> str:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id = %s AND worker_id = %s "
                "AND state = 'running' AND lease_expires_at > clock_timestamp()",
                (task_id, worker_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return "lost"
            return "cancel" if row["cancel_requested"] else "running"

    async def finish(
        self,
        task_id: str,
        worker_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        session_id: str | None = None,
        session_path: str | None = None,
        output_files: list[dict[str, Any]] | None = None,
    ) -> bool:
        if state not in TERMINAL_STATES:
            raise ValueError("Finish requires a terminal state")
        if state == "completed" and (
            result is None or not session_id or not session_path
        ):
            raise ValueError("Completion requires result and session references")
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE tasks SET state = CASE WHEN cancel_requested THEN 'canceled' "
                "ELSE %s END, "
                "result = CASE WHEN cancel_requested THEN NULL ELSE %s END, "
                "error = CASE WHEN cancel_requested THEN NULL ELSE %s END, "
                "session_id = CASE WHEN cancel_requested THEN session_id ELSE %s END, "
                "session_path = CASE WHEN cancel_requested "
                "THEN session_path ELSE %s END, "
                "output_files = CASE WHEN cancel_requested OR %s != 'completed' "
                "THEN '[]'::jsonb ELSE %s END, "
                "lease_expires_at = NULL, updated_at = clock_timestamp() "
                "WHERE id = %s AND worker_id = %s AND state = 'running' "
                "AND lease_expires_at > clock_timestamp() RETURNING context_id, state",
                (
                    state,
                    Jsonb(result),
                    error,
                    session_id,
                    session_path,
                    state,
                    Jsonb(output_files or []),
                    task_id,
                    worker_id,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            if row["state"] == "completed":
                await conn.execute(
                    "UPDATE contexts SET session_id = %s, session_path = %s "
                    "WHERE id = %s",
                    (session_id, session_path, row["context_id"]),
                )
            else:
                await conn.execute(
                    "UPDATE contexts SET usable = false WHERE id = %s",
                    (row["context_id"],),
                )
            return True

    async def reap_expired(self) -> int:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "WITH expired AS (UPDATE tasks SET state = 'failed', "
                "error = 'Worker lease expired; create a new context', "
                "lease_expires_at = NULL, updated_at = clock_timestamp() "
                "WHERE state = 'running' AND lease_expires_at <= clock_timestamp() "
                "RETURNING context_id) "
                "UPDATE contexts SET usable = false WHERE id IN "
                "(SELECT context_id FROM expired)",
            )
            return cursor.rowcount
