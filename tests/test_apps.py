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


# --- Telegram bots ---

class FakeTelegram:
    def __init__(self) -> None:
        self.webhooks: list[tuple[str, str, str]] = []
        self.bad_tokens: set[str] = set()

    async def get_me(self, token):
        from tpf.tools.telegram import TelegramError
        if token in self.bad_tokens:
            raise TelegramError("Telegram getMe: Unauthorized")
        return "glina_test_bot"

    async def set_webhook(self, token, url, secret):
        self.webhooks.append((token, url, secret))


BOT_TOKEN = "123456789:" + "A" * 35
BOT_FILES = APP_CODE["files"] + [{"path": "lib/bot.ts", "content": "export async function handleUpdate(update: unknown): Promise<void> {}"}]


@pytest.fixture
async def bot_env(app_env):
    env = app_env
    env.llm.script["analyst"][0]["spec"] = APP_SPEC | {"product_type": "bot"}
    env.llm.script["developer"] = [{"files": BOT_FILES, "notes": "bot"}]
    env.worker.telegram = FakeTelegram()
    return env


async def enter_token(env, oid, token=BOT_TOKEN):
    """What the portal does when the client submits the token form."""
    from tpf.orchestrator import queue
    await env.pool.execute(
        "INSERT INTO bot_connections (order_id, token, username) VALUES ($1,$2,'glina_test_bot') "
        "ON CONFLICT (order_id) DO UPDATE SET token = EXCLUDED.token, connected_env = NULL", oid, token)
    async with env.pool.acquire() as conn:
        await queue.enqueue(conn, oid, "connect_bot", {})


async def test_bot_is_connected_to_review_then_to_production(bot_env):
    env = bot_env
    tg = env.worker.telegram
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert State(order["state"]) is State.AWAITING_ACCEPTANCE and tg.webhooks == []   # no token yet: nothing to connect

    await enter_token(env, oid)
    await env.drain()
    token, url, secret = tg.webhooks[-1]
    assert token == BOT_TOKEN and url == order["staging_url"] + "/api/tpf-telegram"
    stg = env.deployer.env[f"tpf-order-{oid}-preview"]
    from tpf.tools.telegram import webhook_secret
    assert secret == webhook_secret(stg["SESSION_SECRET"])
    # The application can read the token the platform keeps for it, and cannot change it.
    conn = await asyncpg.connect(stg["DATABASE_URL"])
    assert await conn.fetchval("SELECT value FROM tpf_secrets WHERE key = 'bot_token'") == BOT_TOKEN
    with pytest.raises(asyncpg.PostgresError):
        await conn.execute("UPDATE tpf_secrets SET value = 'x'")
    await conn.close()
    assert "@glina_test_bot" in (await env.orch.messages(oid))[-1]["text"]

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED
    assert tg.webhooks[-1][1] == order["production_url"] + "/api/tpf-telegram"
    assert await env.pool.fetchval("SELECT connected_env FROM bot_connections WHERE order_id = $1", oid) == "prod"


async def test_token_entered_early_is_used_at_the_first_deployment(bot_env):
    env = bot_env
    oid = await new_app_order(env)
    await env.drain()
    await enter_token(env, oid)            # while the estimate is still waiting for approval
    await env.drain()
    assert env.worker.telegram.webhooks == []
    assert "сохранён" in (await env.orch.messages(oid))[-1]["text"]
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert len(env.worker.telegram.webhooks) == 1


async def test_refused_token_tells_the_client_and_keeps_the_order_moving(bot_env):
    env = bot_env
    env.worker.telegram.bad_tokens.add(BOT_TOKEN)
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    await enter_token(env, oid)
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    assert "BotFather" in (await env.orch.messages(oid))[-1]["text"]


def test_static_rules_for_bots():
    from tpf.tools.telegram import TOKEN_RE
    routes = ["/", "/admin", "/admin/login"]
    assert check_files(BOT_FILES, routes, "bot") == []
    assert any("lib/bot.ts" in p for p in check_files(APP_CODE["files"], routes, "bot"))
    direct = BOT_FILES + [{"path": "lib/x.ts", "content": "fetch('https://api.telegram.org/bot' + t)"}]
    assert any("directly" in p for p in check_files(direct, routes, "bot"))
    assert check_schema_sql("CREATE TABLE IF NOT EXISTS tpf_secrets (key text);")
    assert validate_path("app/api/tpf-telegram/route.ts", "bot")
    paths = {f["path"] for f in all_files(BOT_FILES, "x", "bot")}
    assert {"tpf/telegram.ts", "app/api/tpf-telegram/route.ts", "tpf/db.ts"} <= paths
    assert TOKEN_RE.match(BOT_TOKEN) and not TOKEN_RE.match("hello") and not TOKEN_RE.match("123:abc")


async def test_developer_builds_a_project_in_steps_and_patches_on_fixes(env):
    from tpf.llm.gateway import LLMError
    first = {"files": [CODE["files"][0]], "remaining": [{"path": "app/page.tsx", "purpose": "home"}], "notes": "layout"}
    second = {"files": [CODE["files"][1]], "remaining": [], "notes": "home"}
    env.llm.queue("developer", first, second)
    order, _ = await env.orch.create_order(client_name="A", client_email="a@example.com", request_text="site", language="en")
    oid = order["id"]
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    code = await env.orch.latest_artifact(oid, "code")
    assert [f["path"] for f in code["files"]] == ["app/layout.tsx", "app/page.tsx"]
    assert env.llm.calls.count("developer") == 2

    # A change request returns only the changed file; the rest of the project is kept. An answer that
    # runs out of room is asked for again in a smaller piece instead of failing the task.
    original = env.llm.structured
    state = {"failed": False}

    async def flaky(**kw):
        if kw["agent"] == "developer" and not state["failed"]:
            state["failed"] = True
            raise LLMError("developer: output hit max_tokens=32000")
        return await original(**kw)

    env.llm.structured = flaky
    env.llm.queue("developer", {"files": [{"path": "app/page.tsx", "content": "export default function P(){return <main>v2</main>}"}],
                                "delete": [], "remaining": [], "notes": "v2"})
    await env.orch.fire(oid, Ev.CHANGES_REQUESTED, actor="client", data={"comment": "change the heading"})
    await env.drain()
    code = await env.orch.latest_artifact(oid, "code")
    by_path = {f["path"]: f["content"] for f in code["files"]}
    assert "v2" in by_path["app/page.tsx"] and by_path["app/layout.tsx"] == CODE["files"][0]["content"]


def test_each_product_type_has_its_own_rate():
    from tpf.config import Settings
    s = Settings(database_url="postgresql://x/y")
    assert (s.rate_for("site"), s.rate_for("app"), s.rate_for("bot")) == (45, 70, 50)
    assert s.rate_for("something-new") == s.hourly_rate
    assert (s.rate_for("site", "ru"), s.rate_for("app", "ru"), s.rate_for("bot", "ru")) == (36, 56, 34)
    assert s.rate_for("site", "en") == 45 and s.rate_for("bot", "zh") == 50
