"""Scheduled work of delivered applications. An application cannot wake itself, so a worker calls each one that
has scheduled work about every `app_tick_minutes`: the live version of a delivered order, and the review
version while the client is looking at it. The application decides what is due (see SCHEDULE_RULES)."""
from __future__ import annotations

import asyncio
import logging

import asyncpg
import httpx

from tpf.api.apps import app_key
from tpf.config import Settings

log = logging.getLogger("tpf.ticker")
TICK_ROUTE = "/api/tpf-tick"

DUE = """
SELECT o.id, d.env, d.session_secret, CASE WHEN d.env = 'prod' THEN o.production_url ELSE o.staging_url END AS url
FROM orders o
JOIN app_databases d ON d.order_id = o.id
JOIN LATERAL (SELECT content FROM artifacts WHERE order_id = o.id AND kind = 'spec' ORDER BY version DESC LIMIT 1) s ON true
LEFT JOIN app_ticks t ON t.order_id = o.id AND t.env = d.env
WHERE jsonb_typeof(s.content->'capabilities') = 'array' AND s.content->'capabilities' ? 'schedule'
  AND ((d.env = 'prod' AND o.state = 'delivered' AND o.production_url IS NOT NULL)
    OR (d.env = 'stg' AND o.state IN ('awaiting_acceptance', 'awaiting_final_payment') AND o.staging_url IS NOT NULL))
  -- an application that keeps failing is asked less and less often, down to once in about five hours
  AND (t.last_at IS NULL OR t.last_at < now() - make_interval(mins => $1 * (1 + least(t.failures, 20))))
ORDER BY t.last_at NULLS FIRST
LIMIT 40
"""
CLAIM = """
INSERT INTO app_ticks (order_id, env, last_at) VALUES ($1, $2, now())
ON CONFLICT (order_id, env) DO UPDATE SET last_at = now() WHERE app_ticks.last_at < now() - make_interval(mins => $3 - 1)
RETURNING 1
"""


async def tick_due(pool: asyncpg.Pool, settings: Settings, client: httpx.AsyncClient | None = None) -> int:
    """Wake every application whose turn has come. Returns how many were called."""
    rows = await pool.fetch(DUE, settings.app_tick_minutes)
    if not rows:
        return 0
    own = client is None
    client = client or httpx.AsyncClient(timeout=70, follow_redirects=False)
    called = 0
    try:
        for r in rows:
            # Several workers run this loop: the one whose claim lands makes the call.
            if not await pool.fetchval(CLAIM, r["id"], r["env"], settings.app_tick_minutes):
                continue
            called += 1
            try:
                reply = await client.post(r["url"].rstrip("/") + TICK_ROUTE, headers={"x-tpf-app": app_key(r["session_secret"])})
                status = reply.status_code
            except httpx.HTTPError:
                status = 0
            await pool.execute(
                "UPDATE app_ticks SET last_status = $3, failures = CASE WHEN $4 THEN 0 ELSE failures + 1 END "
                "WHERE order_id = $1 AND env = $2", r["id"], r["env"], status, 200 <= status < 300)
            if not 200 <= status < 300:
                log.warning("order %s (%s): scheduled work answered %s", r["id"], r["env"], status)
    finally:
        if own:
            await client.aclose()
    return called


async def run_forever(pool: asyncpg.Pool, settings: Settings, stopping: asyncio.Event) -> None:
    while not stopping.is_set():
        try:
            await tick_due(pool, settings)
        except Exception:  # noqa: BLE001 — a passing database or network error; the next round tries again
            log.exception("scheduled work round failed")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
