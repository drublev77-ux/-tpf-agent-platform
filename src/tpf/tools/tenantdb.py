"""Databases for the web applications the platform builds.

Client applications never touch the platform's own database. They live in a separate "tenant"
Postgres, where every order gets its own schema and its own login role that can see nothing else:
one pair for the review build ('stg') and one for production ('prod').
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg

from tpf.config import Settings


class TenantDBError(RuntimeError):
    pass


@dataclass
class AppDatabase:
    role: str
    database_url: str
    session_secret: str
    admin_password: str

    def env(self) -> dict[str, str]:
        """Environment variables the application needs."""
        return {"DATABASE_URL": self.database_url, "SESSION_SECRET": self.session_secret,
                "ADMIN_PASSWORD": self.admin_password}


# What db/schema.sql may not contain: it runs as the order's own role, which already cannot leave its
# schema; this is the second fence, and it also keeps a redeploy from destroying live data.
_FORBIDDEN = re.compile(
    r"\b(drop|truncate|delete\s+from|grant|revoke|create\s+(role|user|extension|schema|database)|alter\s+(role|user|database|system)"
    r"|set\s+role|set\s+session|copy\s+\w+\s+(from|to)|do\s+\$|create\s+(or\s+replace\s+)?(function|procedure|trigger)"
    r"|security\s+definer)\b", re.I)
_QUALIFIED = re.compile(r"\b(table|index|on|references|into|update)\s+(if\s+not\s+exists\s+)?[a-z_]\w*\.[a-z_]", re.I)


def check_schema_sql(text: str) -> str | None:
    """Return why this schema.sql is refused, or None."""
    bare = re.sub(r"--[^\n]*", "", text)
    bare = re.sub(r"/\*.*?\*/", "", bare, flags=re.S)
    bare = re.sub(r"'(?:[^']|'')*'", "''", bare)          # string literals cannot hide or trigger anything
    m = _FORBIDDEN.search(bare)
    if m:
        return f"db/schema.sql may only create and extend tables and indexes and insert seed rows; found: {m.group(0)!r}"
    if re.search(r"\btpf_\w+", bare, re.I):
        return "db/schema.sql must not create or change tables whose names start with tpf_: the platform owns them"
    if _QUALIFIED.search(bare):
        return "db/schema.sql must not use schema-qualified names"
    return None


class TenantDB:
    def __init__(self, settings: Settings) -> None:
        if not settings.tenant_database_url:
            raise TenantDBError("TENANT_DATABASE_URL is not set")
        self.admin_url = settings.tenant_database_url
        self.parts = urlsplit(self.admin_url)
        # Supabase pooler logins look like "<role>.<project ref>"; a plain Postgres has no suffix.
        user = self.parts.username or ""
        self.suffix = "." + user.split(".", 1)[1] if "." in user else ""

    def _url(self, role: str, password: str) -> str:
        host = self.parts.hostname or ""          # empty: a unix socket named in the query (local development)
        port, query = self.parts.port, self.parts.query
        if host.endswith(".pooler.supabase.com"):
            # Applications run on serverless functions: they use the transaction pooler, over TLS.
            port, query = 6543, "sslmode=require"
        netloc = f"{role}{self.suffix}:{quote(password, safe='')}@{host}" + (f":{port}" if port else "")
        return urlunsplit((self.parts.scheme or "postgresql", netloc, self.parts.path or "/postgres", query, ""))

    async def _connect(self, url: str) -> asyncpg.Connection:
        return await asyncpg.connect(url, statement_cache_size=0, timeout=20)

    async def ensure(self, pool: asyncpg.Pool, order_id: int, env: str) -> AppDatabase:
        """The order's database for this environment, created on first use. Safe to call again."""
        if env not in ("stg", "prod"):
            raise ValueError(env)
        row = await pool.fetchrow("SELECT * FROM app_databases WHERE order_id = $1 AND env = $2", order_id, env)
        fresh = row is None
        if fresh:
            role = f"o{int(order_id)}_{env}"
            row = await pool.fetchrow(
                "INSERT INTO app_databases (order_id, env, role_name, db_password, session_secret, admin_password) "
                "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (order_id, env) DO UPDATE SET role_name = app_databases.role_name RETURNING *",
                order_id, env, role, secrets.token_hex(20), secrets.token_hex(32), secrets.token_urlsafe(9))
        role, password = row["role_name"], row["db_password"]
        if not re.fullmatch(r"o\d+_(stg|prod)", role) or not re.fullmatch(r"[0-9a-f]+", password):
            raise TenantDBError("stored database credentials look wrong")

        admin = await self._connect(self.admin_url)
        try:
            exists = await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
            # Identifiers and the password are generated above from digits and hex only, so they are safe to inline.
            if not exists:
                await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' CONNECTION LIMIT 20")
            elif fresh:
                # The role outlived its record. Otherwise an existing role keeps its password: setting it again
                # re-hashes it, and the connection pooler would keep refusing the login it has cached.
                await admin.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{password}'")
            await admin.execute(f"CREATE SCHEMA IF NOT EXISTS {role}")
            await admin.execute(f"GRANT USAGE, CREATE ON SCHEMA {role} TO {role}")
            await admin.execute(f"ALTER ROLE {role} SET search_path = {role}")
            await admin.execute(f"ALTER ROLE {role} SET statement_timeout = '15s'")
            await admin.execute(f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '30s'")
        finally:
            await admin.close()
        return AppDatabase(role, self._url(role, password), row["session_secret"], row["admin_password"])

    async def set_secrets(self, db: AppDatabase, values: dict[str, str]) -> None:
        """Values the platform keeps for the application (a bot token): a table in the application's schema
        that its role can read but not change."""
        role = db.role
        if not re.fullmatch(r"o\d+_(stg|prod)", role):
            raise TenantDBError("unexpected role name")
        admin = await self._connect(self.admin_url)
        try:
            await admin.execute(f"CREATE TABLE IF NOT EXISTS {role}.tpf_secrets (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            await admin.execute(f"GRANT SELECT ON {role}.tpf_secrets TO {role}")
            for key, value in values.items():
                await admin.execute(
                    f"INSERT INTO {role}.tpf_secrets (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, value)
        finally:
            await admin.close()

    async def get_secrets(self, db: AppDatabase) -> dict[str, str]:
        admin = await self._connect(self.admin_url)
        try:
            if not await admin.fetchval("SELECT to_regclass($1)", f"{db.role}.tpf_secrets"):
                return {}
            return {r["key"]: r["value"] for r in await admin.fetch(f"SELECT key, value FROM {db.role}.tpf_secrets")}
        finally:
            await admin.close()

    async def apply_schema(self, db: AppDatabase, schema_sql: str) -> None:
        """Run the application's schema.sql as the application's own role, inside its own schema."""
        problem = check_schema_sql(schema_sql)
        if problem:
            raise TenantDBError(problem)
        conn = await self._connect(db.database_url)
        try:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL search_path = {db.role}")
                await conn.execute(schema_sql)
        except asyncpg.PostgresError as e:
            raise TenantDBError(f"db/schema.sql failed: {type(e).__name__}: {e}") from e
        finally:
            await conn.close()

    async def selftest(self) -> str:
        """Create a throwaway role and schema, use them the way an application would, and remove them."""
        role, password = f"o0{secrets.randbelow(10**6)}_stg", secrets.token_hex(20)   # unique: workers may start together
        admin = await self._connect(self.admin_url)
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {role} CASCADE")
            await admin.execute(f"DROP ROLE IF EXISTS {role}")
            await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' CONNECTION LIMIT 20")
            await admin.execute(f"CREATE SCHEMA {role}")
            await admin.execute(f"GRANT USAGE, CREATE ON SCHEMA {role} TO {role}")
            await admin.execute(f"ALTER ROLE {role} SET search_path = {role}")
            app = await self._connect(self._url(role, password))
            try:
                async with app.transaction():
                    schema = await app.fetchval("SELECT current_schema()")
                    await app.execute("CREATE TABLE IF NOT EXISTS selftest (id int); INSERT INTO selftest VALUES (1)")
                    rows = await app.fetchval("SELECT count(*) FROM selftest")
            finally:
                await app.close()
            await admin.execute(f"DROP SCHEMA {role} CASCADE")
            await admin.execute(f"DROP ROLE {role}")
        finally:
            await admin.close()
        if schema != role or rows != 1:
            raise TenantDBError(f"self-test: schema {schema!r}, rows {rows!r}")
        return "ok"


def make_tenant_db(settings: Settings) -> Any:
    return TenantDB(settings) if settings.tenant_database_url else None
