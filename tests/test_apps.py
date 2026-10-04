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
APP_LAYOUT = {"path": "app/layout.tsx", "content": "import Pwa from '@/tpf/Pwa';\nexport default function L({children}:{children:React.ReactNode}){return <html><body><Pwa />{children}</body></html>}"}
APP_CODE = {"files": [APP_LAYOUT, CODE["files"][1], {"path": "lib/pwa.ts", "content": "export const PWA = { name: 'A', shortName: 'A', themeColor: '#111111', backgroundColor: '#ffffff', letter: 'A' };"}] + [
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
BOT_FILES = [f for f in APP_CODE["files"] if f["path"] != "lib/pwa.ts"] + [{"path": "lib/bot.ts", "content": "export async function handleUpdate(update: unknown): Promise<void> {}"}]


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
    assert any("lib/pwa.ts" in p for p in check_files(BOT_FILES, routes, "app"))
    assert any("<Pwa />" in p for p in check_files(CODE["files"] + APP_CODE["files"][2:], routes, "app"))
    assert {"app/manifest.ts", "public/sw.js", "tpf/Pwa.tsx"} <= {f["path"] for f in all_files([], "x", "app")}
    assert "app/manifest.ts" not in {f["path"] for f in all_files([], "x", "bot")} and validate_path("app/manifest.ts", "app")
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


# --- online shops ---

class FakeShopStripe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.bad: set[str] = set()

    async def check(self, key):
        from tpf.tools.shop_stripe import StripeKeyError
        if key in self.bad:
            raise StripeKeyError("Invalid API Key")

    async def ensure_webhook(self, key, url, endpoint_id=""):
        from tpf.tools.shop_stripe import StripeKeyError
        if key in self.bad:
            raise StripeKeyError("Invalid API Key")
        self.calls.append((key, url, endpoint_id))
        return (endpoint_id, None) if endpoint_id else (f"we_{len(self.calls)}", f"whsec_{len(self.calls)}")


TEST_KEY, LIVE_KEY = "sk_test_" + "a" * 24, "sk_live_" + "b" * 24
SHOP_FILES = APP_CODE["files"] + [{"path": "lib/payments.ts", "content": "export async function onPaid(reference: string, payment: unknown): Promise<void> {}"}]


@pytest.fixture
async def shop_env(app_env):
    env = app_env
    env.llm.script["analyst"][0]["spec"] = APP_SPEC | {"product_type": "shop"}
    env.llm.script["developer"] = [{"files": SHOP_FILES, "notes": "shop"}]
    env.worker.shop_stripe = FakeShopStripe()
    return env


async def enter_key(env, oid, key):
    from tpf.orchestrator import queue
    from tpf.tools.shop_stripe import key_mode
    await env.pool.execute(
        "INSERT INTO payment_connections (order_id, secret_key, mode) VALUES ($1,$2,$3) "
        "ON CONFLICT (order_id) DO UPDATE SET secret_key = EXCLUDED.secret_key, mode = EXCLUDED.mode, connected_env = NULL",
        oid, key, key_mode(key))
    async with env.pool.acquire() as conn:
        await queue.enqueue(conn, oid, "connect_payments", {})


async def shop_secrets(env, oid, which):
    conn = await asyncpg.connect(env.deployer.env[f"tpf-order-{oid}" + ("-preview" if which == "stg" else "")]["DATABASE_URL"])
    try:
        return {r["key"]: r["value"] for r in await conn.fetch("SELECT key, value FROM tpf_secrets")}
    finally:
        await conn.close()


async def test_shop_payments_follow_the_order_from_review_to_production(shop_env):
    env = shop_env
    stripe = env.worker.shop_stripe
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert State(order["state"]) is State.AWAITING_ACCEPTANCE and stripe.calls == []

    await enter_key(env, oid, TEST_KEY)
    await env.drain()
    assert stripe.calls[-1] == (TEST_KEY, order["staging_url"] + "/api/tpf-stripe", "")
    stg = await shop_secrets(env, oid, "stg")
    assert stg == {"stripe_secret_key": TEST_KEY, "stripe_webhook_id": "we_1", "stripe_webhook_secret": "whsec_1"}
    assert "4242" in (await env.orch.messages(oid))[-1]["text"]

    # A change request redeploys the review build: the same endpoint is moved, its secret is kept.
    env.llm.queue("developer", {"files": [], "remaining": [], "notes": "tweak"})
    await env.orch.fire(oid, Ev.CHANGES_REQUESTED, actor="client", data={"comment": "bigger photos"})
    await env.drain()
    order = await env.orch.get_order(oid)
    assert stripe.calls[-1] == (TEST_KEY, order["staging_url"] + "/api/tpf-stripe", "we_1")
    assert (await shop_secrets(env, oid, "stg"))["stripe_webhook_secret"] == "whsec_1"

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED
    assert stripe.calls[-1] == (TEST_KEY, order["production_url"] + "/api/tpf-stripe", "")   # production gets its own endpoint

    # The owner switches to the live key after release: production is reconnected with a new endpoint.
    await enter_key(env, oid, LIVE_KEY)
    await env.drain()
    prod = await shop_secrets(env, oid, "prod")
    assert prod["stripe_secret_key"] == LIVE_KEY and stripe.calls[-1][2] == ""
    assert await env.pool.fetchval("SELECT mode || ':' || connected_env FROM payment_connections WHERE order_id = $1", oid) == "live:prod"


async def test_live_key_is_never_connected_to_the_review_build(shop_env):
    from tpf.api.portal import T, _pay_block
    env = shop_env
    stripe = env.worker.shop_stripe
    oid = await new_app_order(env)
    await env.drain()
    await enter_key(env, oid, LIVE_KEY)          # before anything is deployed
    await env.drain()
    assert "после запуска" in (await env.orch.messages(oid))[-1]["text"]
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert State(order["state"]) is State.AWAITING_ACCEPTANCE
    # The review build was deployed with a live key on file: Stripe was not touched and the shop has no key.
    assert stripe.calls == []
    conn = await asyncpg.connect(env.deployer.env[f"tpf-order-{oid}-preview"]["DATABASE_URL"])
    assert await conn.fetchval("SELECT to_regclass('tpf_secrets')") is None
    await conn.close()
    assert "не подключается" in await _pay_block(env.orch, oid, "t", T["ru"])

    # Trying payments during the review takes a test key; a live key entered afterwards leaves the review build on it.
    await enter_key(env, oid, TEST_KEY)
    await env.drain()
    assert (await shop_secrets(env, oid, "stg"))["stripe_secret_key"] == TEST_KEY
    await enter_key(env, oid, LIVE_KEY)
    await env.drain()
    assert (await shop_secrets(env, oid, "stg"))["stripe_secret_key"] == TEST_KEY and len(stripe.calls) == 1
    assert "после запуска" in (await env.orch.messages(oid))[-1]["text"]
    assert await env.pool.fetchval("SELECT connected_env FROM payment_connections WHERE order_id = $1", oid) is None

    # The release is where the live key goes.
    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED
    assert stripe.calls[-1] == (LIVE_KEY, order["production_url"] + "/api/tpf-stripe", "")
    assert (await shop_secrets(env, oid, "prod"))["stripe_secret_key"] == LIVE_KEY
    assert await env.pool.fetchval("SELECT mode || ':' || connected_env FROM payment_connections WHERE order_id = $1", oid) == "live:prod"
    held = [e for e in await env.orch.events(oid) if e["name"] == "payments_held"]
    assert held and all(e["data"]["env"] == "stg" for e in held)


async def test_refused_stripe_key_does_not_block_the_shop(shop_env):
    env = shop_env
    env.worker.shop_stripe.bad.add(TEST_KEY)
    oid = await new_app_order(env)
    await env.drain()
    await enter_key(env, oid, TEST_KEY)          # before anything is deployed: just stored
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    assert await env.pool.fetchval("SELECT connected_env FROM payment_connections WHERE order_id = $1", oid) is None


def test_static_rules_for_shops():
    from tpf.tools.shop_stripe import KEY_RE, key_mode
    routes = ["/", "/admin", "/admin/login"]
    assert check_files(SHOP_FILES, routes, "shop") == []
    assert any("lib/payments.ts" in p for p in check_files(APP_CODE["files"], routes, "shop"))
    direct = SHOP_FILES + [{"path": "lib/x.ts", "content": "fetch('https://api.stripe.com/v1/charges')"}]
    assert any("directly" in p for p in check_files(direct, routes, "shop"))
    assert validate_path("app/api/tpf-stripe/route.ts", "shop") and validate_path("app/order/[token]/page.tsx", "shop") is None
    paths = {f["path"] for f in all_files(SHOP_FILES, "x", "shop")}
    assert {"tpf/payments.ts", "app/api/tpf-stripe/route.ts", "tpf/notify.ts"} <= paths
    assert KEY_RE.match(TEST_KEY) and KEY_RE.match("rk_live_" + "c" * 30) and not KEY_RE.match("pk_test_" + "a" * 24)
    assert key_mode(TEST_KEY) == "test" and key_mode(LIVE_KEY) == "live"


def test_use_server_modules_may_only_export_async_functions():
    ok = {"path": "lib/actions.ts", "content": "'use server';\nimport { x } from './x';\nexport async function save() {}\nexport type T = string;\n"}
    for bad in ("export const LIMIT = 5;", "export { x };", "export function plain() {}", "export default function f() {}"):
        broken = {"path": "lib/actions.ts", "content": ok["content"] + bad + "\n"}
        assert any("use server" in p for p in check_files(CODE["files"] + [broken], ["/"], "site")), bad
    assert check_files(CODE["files"] + [ok], ["/"], "site") == []
    assert check_files(CODE["files"] + [{"path": "lib/c.ts", "content": "export const A = 1;"}], ["/"], "site") == []
