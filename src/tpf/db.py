"""Database pool and migrations (asyncpg)."""
from __future__ import annotations

import asyncio
import json
from importlib import resources

import asyncpg

from tpf.config import get_settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    # JSONB columns come back as Python objects and accept dicts/lists directly.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def uses_transaction_pooler(dsn: str) -> bool:
    """Supabase's transaction pooler (Supavisor, port 6543) and PgBouncer in transaction mode
    cannot keep prepared statements between transactions."""
    return ":6543" in dsn or "pgbouncer=true" in dsn


async def create_pool(dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    dsn = dsn or get_settings().database_url
    extra = {"statement_cache_size": 0} if uses_transaction_pooler(dsn) else {}
    return await asyncpg.create_pool(
        dsn.replace("?pgbouncer=true", "").replace("&pgbouncer=true", ""),
        min_size=min_size, max_size=max_size, init=_init_connection, **extra,
    )


async def migrate(pool: asyncpg.Pool) -> list[str]:
    """Apply bundled SQL migrations in name order; each runs once."""
    applied: list[str] = []
    files = sorted(
        (f for f in resources.files("tpf.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for f in files:
            if f.name in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
            applied.append(f.name)
    return applied


def migrate_cli() -> None:
    async def main() -> None:
        pool = await create_pool()
        try:
            applied = await migrate(pool)
            print("applied:", ", ".join(applied) if applied else "nothing new")
        finally:
            await pool.close()

    asyncio.run(main())
