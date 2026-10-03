"""Web applications: an order with a database and an admin area, against a real tenant Postgres."""
from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import ARCH, CODE, SPEC
from tpf.agents.developer import check_files
from tpf.orchestrator.states import Ev, State
from tpf.tools.template import all_files, validate_path
from tpf.tools.tenantdb import TenantDB, check_schema_sql

TENANT_ADMIN = "postgresql://postgres@/tpf_tenant?host=/tmp&port=5433"
SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS bookings (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'new', created_at TIMESTAMPTZ NOT NULL DEFAULT now());\n"
    "CREATE INDEX IF NOT EXISTS bookings_created ON bookings (created_at);\n"
)
APP_SPEC = SPEC | {"product_type": "app", "client_content": [], "missing_content": []}
APP_ARCH = ARCH | {"routes": ARCH["routes"] + [
    {"path": "/admin", "title": "Admin", "purpose": "manage", "sections": ["Bookings"]},
    {"path": "/admin/login", "title": "Login", "purpose": "sign in", "sections": ["Form"]}]}
PAGE = "export default function P(){return <main/>}"
APP_CODE = {"files": CODE["files"] + [
    {"path": "app/admin/page.tsx", "content": PAGE}, {"path": "app/admin/login/page.tsx", "content": PAGE},
    {"path": "db/schema.sql", "content": SCHEMA_SQL}], "notes": "app"}


@pytest.fixture
async def app_env(env):
    try:
        admin = await asyncpg.connect("postgresql://postgres@/postgres?host=/tmp&port=5433")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres not available: {e}")
    await admin.execute("DROP DATABASE IF EXISTS tpf_tenant")
    for r in await admin.fetch("SELECT rolname FROM pg_roles WHERE rolname ~ '^o[0-9]+_(stg|prod)$'"):
        await admin.execute(f"DROP ROLE {r['rolname']}")
    await admin.execute("CREATE DATABASE tpf_tenant")
    await admin.close()
    env.settings.tenant_database_url = TENANT_ADMIN
    env.worker.tenant_db = TenantDB(env.settings)
    analyst = {"in_pilot_scope": True, "questions": [], "spec": APP_SPEC,
               "estimate": {"items": [{"name": "Build", "hours": 20}], "duration_days": 7}}
    env.llm.queue("analyst", analyst)
    env.llm.queue("architect", APP_ARCH)
    env.llm.queue("developer", APP_CODE)
    return env


async def new_app_order(env) -> int:
    order, _ = await env.orch.create_order(
        client_name="Анна", client_email="anna@example.com", request_text="Запись на занятия с админкой", language="ru")
    return order["id"]


async def test_application_gets_its_own_databases_and_admin_passwords(app_env):
    env = app_env
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE

    stg = env.deployer.env[f"tpf-order-{oid}-preview"]
    assert set(stg) == {"DATABASE_URL", "SESSION_SECRET", "ADMIN_PASSWORD"} and f"o{oid}_stg" in stg["DATABASE_URL"]
    assert f"tpf-order-{oid}" not in env.deployer.env        # production is not touched before acceptance

    # The application's role sees its own table and nothing of anyone else.
    conn = await asyncpg.connect(stg["DATABASE_URL"])
    assert await conn.fetchval("SELECT count(*) FROM bookings") == 0
    assert await conn.fetchval("SELECT current_schema()") == f"o{oid}_stg"
    await conn.close()

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED
    prod = env.deployer.env[f"tpf-order-{oid}"]
    assert prod["ADMIN_PASSWORD"] != stg["ADMIN_PASSWORD"] and f"o{oid}_prod" in prod["DATABASE_URL"]
    assert len(env.deployer.health_checks) == 2             # staging and production both answered
    # The deployed project carries the server template, not the static one.
    assert env.deployer.deploys[-1][2] == len(all_files(APP_CODE["files"], "x", "app"))

    other = await asyncpg.connect(prod["DATABASE_URL"])
    with pytest.raises(asyncpg.PostgresError):
        await other.fetchval(f"SELECT count(*) FROM o{oid}_stg.bookings")
    await other.close()


async def test_schema_is_reapplied_without_losing_data(app_env):
    env = app_env
    db = await env.worker.tenant_db.ensure(env.pool, (await new_app_order(env)), "prod")
    await env.worker.tenant_db.apply_schema(db, SCHEMA_SQL)
    conn = await asyncpg.connect(db.database_url)
    await conn.execute("INSERT INTO bookings (name) VALUES ('Анна')")
    await conn.close()
    again = await env.worker.tenant_db.ensure(env.pool, 1, "prod")
    assert again.database_url == db.database_url and again.admin_password == db.admin_password
    await env.worker.tenant_db.apply_schema(again, SCHEMA_SQL + "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS phone TEXT;")
    conn = await asyncpg.connect(db.database_url)
    assert await conn.fetchval("SELECT count(*) FROM bookings") == 1
    await conn.close()


async def test_broken_schema_goes_back_to_the_developer(app_env):
    env = app_env
    bad = {"files": [f if f["path"] != "db/schema.sql" else {"path": f["path"], "content": "CREATE TABLE IF NOT EXISTS a (id BIGINT REFERENCES nowhere(id));"}
                     for f in APP_CODE["files"]], "notes": "bad"}
    env.llm.script["developer"] = [bad, APP_CODE]
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    names = [e["name"] for e in await env.orch.events(oid) if e["kind"] == "transition"]
    assert names.count("checks_failed") == 1


async def test_portal_shows_the_admin_area_only_for_applications(app_env):
    from tpf.api.portal import T, _admin_block
    env = app_env
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    block = await _admin_block(env.orch, oid, "stg", "https://x.vercel.app/", T["ru"])
    assert env.deployer.env[f"tpf-order-{oid}-preview"]["ADMIN_PASSWORD"] in block and "https://x.vercel.app/admin" in block
    assert await _admin_block(env.orch, oid, "prod", "https://x.vercel.app", T["ru"]) == ""   # not released yet
    assert await _admin_block(env.orch, oid + 1, "stg", "https://x.vercel.app", T["en"]) == ""  # a plain site


def test_static_rules_for_applications():
    assert check_schema_sql(SCHEMA_SQL) is None
    assert check_schema_sql("DROP TABLE bookings;") and check_schema_sql("CREATE TABLE public.x (id int);")
    assert check_schema_sql("CREATE EXTENSION pgcrypto;") and check_schema_sql("GRANT ALL ON bookings TO public;")
    assert check_schema_sql("INSERT INTO settings (k, v) VALUES ('note', 'drop by any time; delete from here');") is None
    assert validate_path("db/schema.sql", "app") is None and validate_path("db/schema.sql", "site")
    assert validate_path("app/api/tpf-health/route.ts", "app") and validate_path("db/other.sql", "app")
    routes = ["/", "/admin", "/admin/login"]
    assert check_files(APP_CODE["files"], routes, "app") == []
    leak = APP_CODE["files"] + [{"path": "components/List.tsx", "content": '"use client";\nimport { sql } from "@/tpf/db";'}]
    assert any("client component" in p for p in check_files(leak, routes, "app"))
    unsafe = APP_CODE["files"] + [{"path": "lib/q.ts", "content": "sql.unsafe('select 1')"}]
    assert any("sql.unsafe" in p for p in check_files(unsafe, routes, "app"))
    assert any("db/schema.sql" in p for p in check_files(CODE["files"], ["/"], "app"))
