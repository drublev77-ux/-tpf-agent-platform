"""Applications' requests to the AI model, answered by a worker. Only workers hold the provider's key, so the
web side leaves each request in the database (api/apps.py) and a worker asks the model and writes the answer."""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from tpf.api.apps import ask_model
from tpf.config import Settings

log = logging.getLogger("tpf.ai_relay")

CLAIM = """
UPDATE app_ai_requests SET status = 'running'
WHERE id = (SELECT id FROM app_ai_requests WHERE status = 'queued' AND created_at > now() - interval '1 minute'
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, request
"""


async def answer_one(pool: asyncpg.Pool, settings: Settings) -> bool:
    """Answer one waiting request. Returns False when none waits."""
    row = await pool.fetchrow(CLAIM)
    if row is None:
        return False
    data = await ask_model(settings, row["request"])
    # The request carried photos and documents: they are not kept a moment longer than needed.
    await pool.execute("UPDATE app_ai_requests SET status = 'done', response = $2, request = '{}' WHERE id = $1", row["id"], data)
    return True


async def run_forever(pool: asyncpg.Pool, settings: Settings, stopping: asyncio.Event) -> None:
    rounds = 0
    while not stopping.is_set():
        try:
            busy = await answer_one(pool, settings)
            rounds += 1
            if rounds % 600 == 0:       # whatever nobody came back for (the asking side timed out or restarted)
                await pool.execute("DELETE FROM app_ai_requests WHERE created_at < now() - interval '10 minutes'")
        except Exception:  # noqa: BLE001 — a passing database or network error; the next round tries again
            log.exception("could not answer an application's AI request")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
