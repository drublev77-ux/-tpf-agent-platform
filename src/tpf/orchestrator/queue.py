"""Postgres-backed work queue.

Workers claim one task at a time with FOR UPDATE SKIP LOCKED, so any number of workers can run
side by side. A claimed task holds a short lease that its worker keeps extending while it works; if
the worker dies (a crash, a redeploy), the lease runs out within minutes and another worker picks the
task up again. A worker that is told to stop hands its tasks back at once.
"""
from __future__ import annotations

from typing import Any

import asyncpg

CLAIM_SQL = """
UPDATE tasks
   SET status = 'running',
       attempts = attempts + 1,
       locked_until = now() + make_interval(secs => $1)
 WHERE id = (
        SELECT id FROM tasks
         WHERE (status = 'queued' AND run_after <= now())
            OR (status = 'running' AND locked_until < now())
         ORDER BY run_after, id
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
RETURNING *
"""


async def enqueue(conn: asyncpg.Connection, order_id: int, kind: str, payload: dict[str, Any] | None = None) -> int:
    return await conn.fetchval(
        "INSERT INTO tasks (order_id, kind, payload) VALUES ($1, $2, $3) RETURNING id",
        order_id, kind, payload or {},
    )


async def claim(pool: asyncpg.Pool, lease_sec: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(CLAIM_SQL, lease_sec)


async def extend(pool: asyncpg.Pool, task_id: int, lease_sec: int) -> bool:
    """Keep the lease of a task this worker is still running. False when the task is no longer running
    (finished or cancelled), so there is nothing left to renew."""
    return bool(await pool.fetchval(
        "UPDATE tasks SET locked_until = now() + make_interval(secs => $2) "
        "WHERE id = $1 AND status = 'running' RETURNING 1", task_id, lease_sec))


async def release(pool: asyncpg.Pool, task_ids: list[int]) -> None:
    """Hand running tasks back to the queue now, without waiting for their leases to run out."""
    await pool.execute(
        "UPDATE tasks SET locked_until = now() WHERE id = ANY($1::bigint[]) AND status = 'running'", task_ids)


async def mark_done(conn: asyncpg.Connection, task_id: int, result: dict[str, Any] | None) -> None:
    await conn.execute(
        "UPDATE tasks SET status = 'done', result = $2, locked_until = NULL, finished_at = now() WHERE id = $1",
        task_id, result or {},
    )


async def retry_later(conn: asyncpg.Connection, task_id: int, error: str, delay_sec: int) -> None:
    await conn.execute(
        "UPDATE tasks SET status = 'queued', error = $2, locked_until = NULL, "
        "run_after = now() + make_interval(secs => $3) WHERE id = $1",
        task_id, error, delay_sec,
    )


async def mark_failed(conn: asyncpg.Connection, task_id: int, error: str) -> None:
    await conn.execute(
        "UPDATE tasks SET status = 'failed', error = $2, locked_until = NULL, finished_at = now() WHERE id = $1",
        task_id, error,
    )


async def cancel_live(conn: asyncpg.Connection, order_id: int, except_task: int | None = None) -> None:
    await conn.execute(
        "UPDATE tasks SET status = 'cancelled', locked_until = NULL, finished_at = now() "
        "WHERE order_id = $1 AND status IN ('queued', 'running') AND id IS DISTINCT FROM $2",
        order_id, except_task,
    )
