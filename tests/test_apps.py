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
APP_LAYOUT = {"path": "app/layout.tsx", "content": "import Pwa from '@/tpf/Pwa';\nimport { siteMetadata } from '@/tpf/seo';\nexport const metadata = siteMetadata();\nexport default function L({children}:{children:React.ReactNode}){return <html><body><Pwa />{children}</body></html>}"}
APP_CODE = {"files": [APP_LAYOUT, CODE["files"][1], CODE["files"][2], {"path": "lib/pwa.ts", "content": "export const PWA = { name: 'A', shortName: 'A', themeColor: '#111111', backgroundColor: '#ffffff', letter: 'A' };"}] + [
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
    assert {"app/manifest.ts", "public/sw.js", "tpf/Pwa.tsx"} <= {f["path"] for f in all_files(APP_CODE["files"], "x", "app")}
    # an application delivered before applications became installable still builds on redeploy
    assert "app/manifest.ts" not in {f["path"] for f in all_files(BOT_FILES, "x", "app")}
    assert "app/manifest.ts" not in {f["path"] for f in all_files(APP_CODE["files"], "x", "bot")} and validate_path("app/manifest.ts", "app")
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
    second = {"files": CODE["files"][1:], "remaining": [], "notes": "home"}
    env.llm.queue("developer", first, second)
    order, _ = await env.orch.create_order(client_name="A", client_email="a@example.com", request_text="site", language="en")
    oid = order["id"]
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    code = await env.orch.latest_artifact(oid, "code")
    assert [f["path"] for f in code["files"]] == ["app/layout.tsx", "app/page.tsx", "lib/seo.ts"]
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
    assert (s.rate_for("site"), s.rate_for("app"), s.rate_for("bot")) == (44, 63, 45)
    assert s.rate_for("something-new") == s.hourly_rate
    assert (s.rate_for("site", "ru"), s.rate_for("app", "ru"), s.rate_for("bot", "ru")) == (44, 50, 30)
    assert s.rate_for("site", "en") == 44 and s.rate_for("bot", "zh") == 45 and s.currency == "EUR"


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


ON_PAID = (
    "export async function onPaid(reference: string, payment: { amountTotal: number }): Promise<void> { void payment.amountTotal; }\n"
    "export async function createCheckOrder() { return { reference: 'r', amountTotal: 1, currency: 'usd' }; }\n"
    "export async function isPaid(reference: string): Promise<boolean> { return false; }\n"
    "export async function removeCheckOrder(reference: string): Promise<void> {}\n")
TEST_KEY, LIVE_KEY = "sk_test_" + "a" * 24, "sk_live_" + "b" * 24
SHOP_FILES = APP_CODE["files"] + [{"path": "lib/payments.ts", "content": ON_PAID}]


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
    assert stg == {"pay_provider": "stripe", "stripe_secret_key": TEST_KEY, "stripe_webhook_id": "we_1", "stripe_webhook_secret": "whsec_1"}
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


async def test_pages_that_crash_when_opened_send_the_code_back(app_env):
    """With Vercel as the build sandbox, QA opens every page of the built application as the owner."""
    env = app_env
    env.settings.sandbox = "vercel"
    env.deployer.smoke_problems = [["/admin: server error 500"]]
    env.llm.script["developer"] = [APP_CODE, {"files": [], "remaining": [], "notes": "fixed"}]
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    names = [e["name"] for e in await env.orch.events(oid) if e["kind"] == "transition"]
    assert names.count("checks_failed") == 1
    url, routes, signed_in = env.deployer.smoked[0]
    assert routes == ["/", "/admin", "/admin/login"] and signed_in


def test_admin_cookie_matches_the_template_signature():
    import hashlib
    import hmac

    from tpf.agents.qa import admin_cookie
    exp, sig = admin_cookie("secret").split(".")
    assert sig == hmac.new(b"secret", f"admin:{exp}".encode(), hashlib.sha256).hexdigest() and int(exp) > 0


# --- shops paid through YooKassa (Russia) or bePaid (Belarus) ---

YK_TEST, YK_LIVE, BEPAID_KEY = "test_" + "y" * 40, "live_" + "y" * 40, "b" * 64


async def enter_provider_key(env, oid, provider, shop_id, key):
    """What the portal does when the owner submits the payment form with YooKassa or bePaid chosen."""
    from tpf.orchestrator import queue
    from tpf.tools.shop_pay import mode_of
    await env.pool.execute(
        "INSERT INTO payment_connections (order_id, secret_key, mode, provider, account_id) VALUES ($1,$2,$3,$4,$5) "
        "ON CONFLICT (order_id) DO UPDATE SET secret_key = EXCLUDED.secret_key, mode = EXCLUDED.mode, "
        "provider = EXCLUDED.provider, account_id = EXCLUDED.account_id, connected_env = NULL",
        oid, key, mode_of(provider, key), provider, shop_id)
    async with env.pool.acquire() as conn:
        await queue.enqueue(conn, oid, "connect_payments", {})


async def test_yookassa_shop_test_key_for_review_live_key_for_release(shop_env):
    env = shop_env
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    await enter_provider_key(env, oid, "yookassa", "100500", YK_TEST)
    await env.drain()
    assert await shop_secrets(env, oid, "stg") == {"pay_provider": "yookassa", "yookassa_shop_id": "100500", "yookassa_secret_key": YK_TEST}
    assert env.worker.shop_stripe.calls == []                       # nothing is registered with anyone
    said = (await env.orch.messages(oid))[-1]["text"]
    assert "ЮKassa" in said and "тестовом режиме" in said and "4242" not in said

    # The shop may note its payments and read them back, and nothing more.
    conn = await asyncpg.connect(env.deployer.env[f"tpf-order-{oid}-preview"]["DATABASE_URL"])
    await conn.execute("INSERT INTO tpf_payments (payment_id, provider, reference) VALUES ('p1', 'yookassa', 'ref-1')")
    assert await conn.fetchval("SELECT reference FROM tpf_payments WHERE payment_id = 'p1'") == "ref-1"
    for forbidden in ("UPDATE tpf_payments SET reference = 'x'", "DELETE FROM tpf_payments", "DROP TABLE tpf_payments"):
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(forbidden)
    await conn.close()

    await enter_provider_key(env, oid, "yookassa", "100501", YK_LIVE)   # the live shop's key, still under review
    await env.drain()
    assert (await shop_secrets(env, oid, "stg"))["yookassa_secret_key"] == YK_TEST
    assert "после запуска" in (await env.orch.messages(oid))[-1]["text"]

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED
    assert await shop_secrets(env, oid, "prod") == {"pay_provider": "yookassa", "yookassa_shop_id": "100501", "yookassa_secret_key": YK_LIVE}
    assert await env.pool.fetchval("SELECT provider || ':' || mode || ':' || connected_env FROM payment_connections WHERE order_id = $1", oid) == "yookassa:live:prod"

    # Entered again after release, the owner is told where YooKassa should send its notifications.
    await enter_provider_key(env, oid, "yookassa", "100501", YK_LIVE)
    await env.drain()
    said = (await env.orch.messages(oid))[-1]["text"]
    assert order["production_url"] + "/api/tpf-pay" in said and "payment.succeeded" in said


async def test_bepaid_shop_pays_in_test_mode_under_review_and_for_real_after_release(shop_env):
    from tpf.api.portal import T, _pay_block
    env = shop_env
    oid = await new_app_order(env)
    await env.drain()
    await enter_provider_key(env, oid, "bepaid", "361", BEPAID_KEY)   # before anything is deployed
    await env.drain()
    assert "bePaid" in (await env.orch.messages(oid))[-1]["text"] and "сохранены" in (await env.orch.messages(oid))[-1]["text"]
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    stg = await shop_secrets(env, oid, "stg")                         # the same key, marked as test for the review build
    assert stg == {"pay_provider": "bepaid", "bepaid_shop_id": "361", "bepaid_secret_key": BEPAID_KEY, "bepaid_test": "1"}
    block = await _pay_block(env.orch, oid, "t", T["ru"])
    assert "bePaid" in block and "Тестовый режим" in block and BEPAID_KEY not in block

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    assert (await env.orch.get_order(oid))["state"] == State.DELIVERED
    assert (await shop_secrets(env, oid, "prod"))["bepaid_test"] == "0"
    assert "Рабочий режим" in await _pay_block(env.orch, oid, "t", T["ru"])

    # Switching the provider later replaces which one the shop uses.
    await enter_provider_key(env, oid, "stripe", "", LIVE_KEY)
    await env.drain()
    prod = await shop_secrets(env, oid, "prod")
    assert prod["pay_provider"] == "stripe" and prod["stripe_secret_key"] == LIVE_KEY


async def test_provider_credentials_are_checked_without_leaking_them():
    import base64
    import httpx
    from tpf.tools.shop_pay import ShopPay, ShopPayError, effective_mode, mode_of, valid
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        user, _, password = base64.b64decode(request.headers["authorization"].split()[1]).decode().partition(":")
        seen.append((request.method, str(request.url), user))
        if request.url.host == "api.yookassa.ru":
            return httpx.Response(200 if password == YK_TEST else 401, json={"account_id": user, "test": True})
        body = __import__("json").loads(request.content)
        assert body["checkout"]["test"] is True and request.headers["x-api-version"] == "2"   # the probe never asks for real money
        return httpx.Response(201 if password == BEPAID_KEY else 401, json={"checkout": {"token": "t"}})

    api = ShopPay(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await api.check("yookassa", "100500", YK_TEST)
    await api.check("bepaid", "361", BEPAID_KEY)
    assert seen == [("GET", "https://api.yookassa.ru/v3/me", "100500"), ("POST", "https://checkout.bepaid.by/ctp/api/checkouts", "361")]
    for provider, shop, key in (("yookassa", "100500", YK_LIVE), ("bepaid", "361", "c" * 64)):
        with pytest.raises(ShopPayError) as e:
            await api.check(provider, shop, key)
        assert key not in str(e.value)
    # bePaid answering 422 means the keys are fine and only the probe order was not: accepted.
    await ShopPay(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(422, json={})))).check("bepaid", "361", BEPAID_KEY)
    down = ShopPay(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={}))))
    with pytest.raises(ShopPayError):
        await down.check("bepaid", "361", BEPAID_KEY)

    assert valid("yookassa", "100500", YK_TEST) and valid("bepaid", "361", BEPAID_KEY)
    assert not valid("yookassa", "100500", "sk_test_" + "a" * 24) and not valid("yookassa", "", YK_TEST)
    assert not valid("bepaid", "abc", BEPAID_KEY) and not valid("stripe", "1", YK_TEST) and not valid("paypal", "1", BEPAID_KEY)
    assert (mode_of("yookassa", YK_TEST), mode_of("yookassa", YK_LIVE), mode_of("bepaid", BEPAID_KEY)) == ("test", "live", "live")
    assert (effective_mode("bepaid", "live", "stg"), effective_mode("bepaid", "live", "prod")) == ("test", "live")
    assert effective_mode("yookassa", "live", "stg") == "live" and effective_mode("stripe", "test", "prod") == "test"


def test_shop_template_carries_all_three_providers_and_keeps_them_out_of_generated_code():
    from tpf.tools.template import rules_for
    files = {f["path"]: f["content"] for f in all_files(SHOP_FILES, "x", "shop")}
    assert {"tpf/payments.ts", "app/api/tpf-stripe/route.ts", "app/api/tpf-pay/route.ts", "app/api/tpf-pay-return/route.ts"} <= set(files)
    assert all(host in files["tpf/payments.ts"] for host in ("api.stripe.com", "api.yookassa.ru", "checkout.bepaid.by"))
    assert "app/api/tpf-pay/route.ts" not in {f["path"] for f in all_files(APP_CODE["files"], "x", "app")}
    for path in ("app/api/tpf-pay/route.ts", "app/api/tpf-pay-return/route.ts", "app/api/tpf-stripe/route.ts"):
        assert validate_path(path, "shop")
    routes = ["/", "/admin", "/admin/login"]
    for host in ("api.yookassa.ru", "checkout.bepaid.by", "api.stripe.com"):
        direct = SHOP_FILES + [{"path": "lib/x.ts", "content": f"fetch('https://{host}/v3/payments')"}]
        assert any("payment provider" in p for p in check_files(direct, routes, "shop"))
    assert check_schema_sql("CREATE TABLE IF NOT EXISTS tpf_payments (id int);")      # the platform's table, not the developer's
    rules = rules_for("shop")
    assert "YooKassa" in rules and "never names one" in rules and "app/api/tpf-pay-return" in rules


def test_onpaid_must_read_the_payment_fields_as_typed():
    """A delivered shop refused every real Stripe payment: its onPaid read `amount` through a cast, got
    undefined, and recorded 'paid 0'. The fields are named in the rules and checked before QA."""
    from tpf.tools.template import rules_for
    routes = ["/", "/admin", "/admin/login"]
    guessed = "export async function onPaid(reference: string, payment: Payment): Promise<void> { const p = payment as unknown as { amount?: number }; void p.amount; }"
    cast = "export async function onPaid(reference: string, payment: Payment): Promise<void> { const p = payment as any; void p.amountTotal; }"
    exports = ON_PAID.split("\n", 1)[1]
    for bad, why in ((guessed, "amountTotal"), (cast, "without")):
        files = [f if f["path"] != "lib/payments.ts" else {"path": f["path"], "content": bad + "\n" + exports} for f in SHOP_FILES]
        assert not any("must export" in p for p in check_files(files, routes, "shop"))
        assert any(why in p for p in check_files(files, routes, "shop")), why
    assert check_files(SHOP_FILES, routes, "shop") == []
    rules = rules_for("shop")
    assert "amountTotal: number" in rules and "lowercase ISO code" in rules
    payments_ts = {f["path"]: f["content"] for f in all_files(SHOP_FILES, "x", "shop")}["tpf/payments.ts"]
    assert payments_ts.count(".toLowerCase(), email") == 2          # YooKassa and bePaid report the currency as Stripe does


async def test_delivered_order_is_redeployed_without_moving_or_notifying(shop_env):
    from tpf.orchestrator import queue
    env = shop_env
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()

    async def redeploy():
        async with env.pool.acquire() as conn:
            await queue.enqueue(conn, oid, "redeploy_production", {"note": "payments fix"})
        await env.drain()
        return await env.pool.fetchval("SELECT result->>'result' FROM tasks WHERE order_id = $1 ORDER BY id DESC LIMIT 1", oid)

    assert await redeploy() == "not_delivered"                 # under review: nothing is touched
    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    await enter_key(env, oid, TEST_KEY)
    await env.drain()
    order = await env.orch.get_order(oid)
    messages, deploys = len(await env.orch.messages(oid)), len(env.deployer.deploys)

    assert await redeploy() == "redeployed"
    after = await env.orch.get_order(oid)
    assert after["state"] == State.DELIVERED and after["production_url"] == order["production_url"]
    assert env.deployer.deploys[-1][:2] == (f"tpf-order-{oid}", True) and len(env.deployer.deploys) == deploys + 1
    assert len(await env.orch.messages(oid)) == messages       # the client hears nothing
    assert env.worker.shop_stripe.calls[-1][1] == order["production_url"] + "/api/tpf-stripe"   # payments stay connected
    assert [e["data"]["note"] for e in await env.orch.events(oid) if e["name"] == "redeployed"] == ["payments fix"]

    # A build that fails leaves the live version alone and tells the operator, not the client.
    from tpf.tools.deploy import DeployError
    env.deployer.failures.append(DeployError("deployment dpl_x ended in ERROR", log="Type error", deployment_id="dpl_x"))
    assert await redeploy() == "failed"
    assert (await env.orch.get_order(oid))["state"] == State.DELIVERED and len(await env.orch.messages(oid)) == messages


async def test_shop_must_pass_the_payment_check_before_the_client_sees_it(shop_env):
    """QA has the built shop pay throwaway orders through its own onPaid; a shop that fails goes back to the developer."""
    env = shop_env
    env.settings.sandbox = "vercel"
    env.deployer.payment_problems = [["a payment of the order's exact total and currency marks it paid, and a repeat changes nothing: "
                                      "isPaid is false after onPaid with the order's exact total and currency"]]
    env.llm.queue("developer", {"files": [], "remaining": [], "notes": "fixed onPaid"})
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    names = [e["name"] for e in await env.orch.events(oid) if e["kind"] == "transition"]
    assert names.count("checks_failed") == 1                       # the first build was stopped by the payment check
    reports = [r["content"] for r in await env.pool.fetch("SELECT content FROM artifacts WHERE order_id = $1 AND kind = 'qa_report' ORDER BY version", oid)]
    assert reports[0]["payment_check"]["ok"] is False and "isPaid is false" in reports[0]["build"]["log_tail"]
    assert reports[0]["build"]["failed_at"] == "health" and "preview" not in reports[0]      # a failed build is never shown as staging
    assert reports[-1]["payment_check"] == {"ok": True, "failed": []} and reports[-1]["passed"]

    # Each check had its own long token, the shop was asked at the build's own address, and the token is gone afterwards.
    (url1, token1), (url2, token2) = env.deployer.payment_checks
    assert token1 != token2 and len(token1) == 48 and url2 == reports[-1]["preview"]["url"]
    assert (await shop_secrets(env, oid, "stg"))["check_token"] == ""


async def test_plain_applications_are_not_asked_for_a_payment_check(app_env):
    env = app_env
    env.settings.sandbox = "vercel"
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE and env.deployer.payment_checks == []


def test_shop_code_must_export_the_check_functions():
    routes = ["/", "/admin", "/admin/login"]
    only_on_paid = [f if f["path"] != "lib/payments.ts" else {"path": f["path"], "content": ON_PAID.split("\n", 1)[0]} for f in SHOP_FILES]
    problems = check_files(only_on_paid, routes, "shop")
    assert [n for n in ("createCheckOrder", "isPaid", "removeCheckOrder") if any(n in p for p in problems)] == ["createCheckOrder", "isPaid", "removeCheckOrder"]
    assert validate_path("app/api/tpf-check/route.ts", "shop")
    files = {f["path"]: f["content"] for f in all_files(SHOP_FILES, "x", "shop")}
    assert "check_token" in files["app/api/tpf-check/route.ts"] and "__tpfMuteNotify" in files["tpf/notify.ts"]
    assert "app/api/tpf-check/route.ts" not in {f["path"] for f in all_files(APP_CODE["files"], "x", "app")}


# --- applications whose people sign in, each with a role -------------------------------------------

ROLES = [{"id": "worker", "name": "Рабочий", "sees": "own tasks", "can": "tick tasks"},
         {"id": "manager", "name": "Менеджер", "sees": "everything", "can": "everything"}]
ACC_SPEC = APP_SPEC | {"user_roles": [{"name": r["name"], "sees": r["sees"], "can": r["can"]} for r in ROLES]}
_route = lambda path, access: {"path": path, "title": path, "purpose": "x", "sections": ["x"], "access": access}   # noqa: E731
ACC_ARCH = ARCH | {"roles": ROLES, "routes": [
    _route("/", "public"), _route("/login", "public"), _route("/account", "users"), _route("/tasks", "worker, manager"),
    _route("/manage", "manager"), _route("/admin", "owner"), _route("/admin/login", "public"), _route("/admin/users", "owner")]}
_page = lambda guard: f"import {{ x }} from '@/tpf/y';\nexport default async function P(){{ {guard}; return <main/> }}"   # noqa: E731
ACC_CODE = {"files": [f for f in APP_CODE["files"] if not f["path"].startswith("app/admin")] + [
    {"path": "lib/roles.ts", "content": 'export const ROLES: { id: string; name: string }[] = [{ id: "worker", name: "Рабочий" }, { id: "manager", name: "Менеджер" }];'},
    {"path": "app/login/page.tsx", "content": PAGE}, {"path": "app/admin/login/page.tsx", "content": PAGE},
    {"path": "app/account/page.tsx", "content": _page("await requireUser()")},
    {"path": "app/tasks/page.tsx", "content": _page("await requireUser('worker', 'manager')")},
    {"path": "app/manage/page.tsx", "content": _page("await requireUser('manager')")},
    {"path": "app/admin/page.tsx", "content": _page("await requireAdmin()")},
    {"path": "app/admin/users/page.tsx", "content": _page("await requireAdmin()")}], "notes": "accounts"}


def test_architecture_with_accounts_must_say_who_opens_what():
    from tpf.agents.architect import access_problems

    assert access_problems(ACC_ARCH) == []
    broken = ACC_ARCH | {"routes": [r for r in ACC_ARCH["routes"] if r["path"] != "/account"]
                         + [_route("/reports", "boss"), _route("/admin/extra", "manager"), _route("/open", "")]}
    text = " | ".join(access_problems(broken))
    assert "/account is missing" in text and "/reports" in text and "/admin/extra" in text and "/open has no access" in text
    assert "roles is empty" in " ".join(access_problems(ACC_ARCH | {"roles": []}))


def test_code_with_accounts_is_checked_before_qa():
    from tpf.agents.developer import check_accounts

    assert check_accounts(ACC_CODE["files"], ACC_ARCH) == []
    files = [f for f in ACC_CODE["files"] if f["path"] not in ("app/manage/page.tsx", "lib/roles.ts")] + [
        {"path": "lib/roles.ts", "content": 'export const ROLES = [{ id: "worker", name: "Рабочий" }];'},
        {"path": "app/manage/page.tsx", "content": PAGE},
        {"path": "lib/auth.ts", "content": "import { scrypt } from 'crypto';\nconst q = sql`select * from tpf_users`;"},
        {"path": "db/schema.sql", "content": SCHEMA_SQL + "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS password TEXT;"}]
    text = " | ".join(check_accounts(files, ACC_ARCH))
    assert "does not list the role 'manager'" in text and "app/manage/page.tsx must call requireUser" in text
    assert "only through @/tpf/users" in text and "hashed by the platform" in text and "must not store passwords" in text


def test_every_page_is_tried_as_a_stranger_and_as_each_role():
    from tpf.agents.qa import access_cases

    cookies = {"worker": {"tpf_user": "w"}, "manager": {"tpf_user": "m"}}
    cases = access_cases(ACC_ARCH["routes"], ["worker", "manager"], cookies)
    got = {(route, who): opens for route, _c, opens, who in cases}
    assert got[("/manage", "role worker")] is False and got[("/manage", "role manager")] is True
    assert got[("/tasks", "role worker")] is True and got[("/account", "a visitor who is not signed in")] is False
    assert got[("/admin/users", "role manager")] is False and not any(route in ("/", "/login", "/admin/login") for route, _ in got)


async def test_application_with_accounts_gets_a_users_table_and_an_access_check(app_env):
    env = app_env
    env.settings.sandbox = "vercel"
    for agent in ("analyst", "architect", "developer"):
        env.llm.script[agent].clear()
    env.llm.queue("analyst", {"in_pilot_scope": True, "questions": [], "spec": ACC_SPEC,
                              "estimate": {"items": [{"name": "Build", "hours": 40}], "duration_days": 10}})
    env.llm.queue("architect", ACC_ARCH)
    env.llm.queue("developer", ACC_CODE, ACC_CODE)
    env.deployer.access_problems = [["/manage as role worker: opens, although role worker must not see this page"]]
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    reports = [r["content"] for r in await env.pool.fetch(
        "SELECT content FROM artifacts WHERE order_id=$1 AND kind='qa_report' ORDER BY version", oid)]
    assert not reports[0]["passed"] and "role worker" in reports[0]["build"]["log_tail"]       # went back to the developer
    assert reports[-1]["passed"] and reports[-1]["access_check"] == {"ok": True, "failed": []}
    assert len(env.deployer.access_cases) == 15
    assert (await env.orch.get_order(oid))["token_budget"] == env.settings.order_token_budget_accounts   # more room than a plain app
    # the owner's smoke test opened only public and admin pages
    assert set(env.deployer.smoked[-1][1]) == {"/", "/login", "/admin", "/admin/login", "/admin/users"}

    stg = env.deployer.env[f"tpf-order-{oid}-preview"]
    conn = await asyncpg.connect(stg["DATABASE_URL"])
    assert await conn.fetchval("SELECT count(*) FROM tpf_users") == 0            # the check's accounts are gone
    await conn.execute("INSERT INTO tpf_users (email, name, role, password_hash) VALUES ('a@b.co', 'A', 'worker', 'x')")
    with pytest.raises(asyncpg.PostgresError):
        await conn.execute("DELETE FROM tpf_users")                              # people are blocked, never deleted
    await conn.close()
    # the deployed project carries the platform's accounts module
    assert env.deployer.deploys[-1][2] == len(all_files(ACC_CODE["files"], "x", "app"))
    assert "tpf/users.ts" in {f["path"] for f in all_files(ACC_CODE["files"], "x", "app")}
    assert "tpf/users.ts" not in {f["path"] for f in all_files(APP_CODE["files"], "x", "app")}


async def test_architect_drops_roles_when_the_spec_has_no_accounts(app_env):
    env = app_env
    env.llm.script["architect"].clear()
    env.llm.queue("architect", APP_ARCH | {"roles": ROLES})
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert "roles" not in await env.orch.latest_artifact(oid, "architecture")


def test_client_components_may_import_account_types_but_not_functions():
    from tpf.agents.developer import check_accounts

    row = lambda body: ACC_CODE["files"] + [{"path": "app/admin/users/UserRow.tsx", "content": '"use client";\n' + body}]   # noqa: E731
    assert check_accounts(row('import type { ManagedUser } from "@/tpf/users";'), ACC_ARCH) == []
    assert check_accounts(row('import { type ManagedUser } from "@/tpf/users";'), ACC_ARCH) == []
    assert "server-only" in " ".join(check_accounts(row('import { roleName, type User } from "@/tpf/users";'), ACC_ARCH))


async def test_application_with_roles_gets_more_check_rounds(app_env):
    env = app_env
    env.settings.sandbox = "vercel"
    env.settings.max_check_attempts, env.settings.max_check_attempts_accounts = 2, 4
    for agent in ("analyst", "architect", "developer"):
        env.llm.script[agent].clear()
    env.llm.queue("analyst", {"in_pilot_scope": True, "questions": [], "spec": ACC_SPEC,
                              "estimate": {"items": [{"name": "Build", "hours": 40}], "duration_days": 10}})
    env.llm.queue("architect", ACC_ARCH)
    env.llm.queue("developer", *[ACC_CODE] * 6)
    wrong = ["/manage as role worker: opens, although role worker must not see this page"]
    env.deployer.access_problems = [wrong, wrong, wrong]          # three failed rounds: more than a plain order is allowed
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE


# --- files, chat and location in applications with accounts ---------------------------------------

def test_capabilities_come_only_from_an_app_spec_and_bring_their_rules():
    from tpf.tools.template import capabilities_of, capability_rules, platform_files

    spec = ACC_SPEC | {"capabilities": ["geolocation", "uploads", "telepathy"]}
    assert capabilities_of(spec) == ["uploads", "geolocation"]
    assert capabilities_of(spec | {"product_type": "shop"}) == [] and capabilities_of(ACC_SPEC) == []
    rules = capability_rules(spec)
    assert "saveFile" in rules and "GeoForm" in rules and "AutoRefresh" not in rules
    files = platform_files("", "app")
    assert {"tpf/files.ts", "tpf/FileInput.tsx", "tpf/AutoRefresh.tsx", "tpf/GeoForm.tsx", "tpf/geo.ts"} <= set(files)
    assert "bodySizeLimit" in files["next.config.mjs"] and "tpf/files.ts" not in platform_files("", "site")


def test_code_may_not_serve_files_blindly_or_read_location_itself():
    from tpf.agents.developer import check_accounts

    extra = lambda path, body: ACC_CODE["files"] + [{"path": path, "content": body}]   # noqa: E731
    open_route = "import { serveFile } from '@/tpf/files';\nexport async function GET() { return serveFile('x'); }"
    assert "without checking who asks" in " ".join(check_accounts(extra("app/files/[id]/route.ts", open_route), ACC_ARCH))
    guarded = "import { currentUser } from '@/tpf/users';\n" + open_route.replace("return", "if (!(await currentUser())) return new Response('', { status: 404 }); return")
    assert check_accounts(extra("app/files/[id]/route.ts", guarded), ACC_ARCH) == []
    assert "through GeoForm" in " ".join(check_accounts(extra("components/Where.tsx", '"use client";\nnavigator.geolocation.getCurrentPosition(() => {});'), ACC_ARCH))
    files = [f if f["path"] != "db/schema.sql" else {"path": "db/schema.sql", "content": SCHEMA_SQL + "CREATE TABLE IF NOT EXISTS docs (id SERIAL PRIMARY KEY, body BYTEA);"} for f in ACC_CODE["files"]]
    assert "bytea" in " ".join(check_accounts(files, ACC_ARCH))


async def test_application_that_stores_files_gets_the_platforms_file_table(app_env):
    env = app_env
    env.settings.sandbox = "vercel"
    for agent in ("analyst", "architect", "developer"):
        env.llm.script[agent].clear()
    env.llm.queue("analyst", {"in_pilot_scope": True, "questions": [], "spec": ACC_SPEC | {"capabilities": ["uploads"]},
                              "estimate": {"items": [{"name": "Build", "hours": 50}], "duration_days": 10}})
    env.llm.queue("architect", ACC_ARCH)
    route = ("import { currentUser } from '@/tpf/users';\nimport { serveFile } from '@/tpf/files';\n"
             "export async function GET() { if (!(await currentUser())) return new Response('', { status: 404 }); return serveFile('x'); }")
    env.llm.queue("developer", {"files": ACC_CODE["files"] + [{"path": "app/files/[id]/route.ts", "content": route}], "notes": "files"})
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    conn = await asyncpg.connect(env.deployer.env[f"tpf-order-{oid}-preview"]["DATABASE_URL"])
    await conn.execute("INSERT INTO tpf_files (id, name, mime, size, data) VALUES ('a', 'r.jpg', 'image/jpeg', 3, $1)", b"abc")
    assert await conn.fetchval("SELECT data FROM tpf_files WHERE id = 'a'") == b"abc"
    await conn.execute("DELETE FROM tpf_files WHERE id = 'a'")
    await conn.close()


async def test_a_fix_that_takes_several_steps_keeps_the_report_in_front_of_the_developer(app_env):
    env = app_env
    env.settings.sandbox = "vercel"
    for agent in ("analyst", "architect", "developer"):
        env.llm.script[agent].clear()
    env.llm.queue("analyst", {"in_pilot_scope": True, "questions": [], "spec": ACC_SPEC,
                              "estimate": {"items": [{"name": "Build", "hours": 40}], "duration_days": 10}})
    env.llm.queue("architect", ACC_ARCH)
    later = {"path": "app/manage/page.tsx", "purpose": "the manager's page"}
    env.llm.queue("developer", ACC_CODE,
                  {"files": [f for f in ACC_CODE["files"] if f["path"] == "app/tasks/page.tsx"], "remaining": [later], "notes": ""},
                  {"files": [f for f in ACC_CODE["files"] if f["path"] == later["path"]], "remaining": [], "notes": ""})
    env.deployer.access_problems = [["/manage as role worker: opens, although role worker must not see this page"]]
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    asked = [p for agent, p in env.llm.prompts if agent == "developer"]
    assert len(asked) == 3 and "FAILED checks" not in asked[0]
    for prompt in asked[1:]:                       # the second step of the fix used to be asked blind
        assert "FAILED checks" in prompt and "role worker must not see this page" in prompt
    assert "Continue: write the next files" in asked[2]


async def test_minor_findings_do_not_cost_a_round_of_fixes(app_env):
    env = app_env
    env.llm.queue("reviewer", {"approved": True, "issues": []}, {"approved": True, "issues": []},     # spec, architecture
                  {"issues": [], "minor": ["the status select has no label"]})                        # the finished code
    oid = await new_app_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ACCEPTANCE
    report = await env.orch.latest_artifact(oid, "qa_report")
    assert report["passed"] and report["acceptance"] == {"ok": True, "issues": [], "minor": ["the status select has no label"]}
    assert env.llm.calls.count("developer") == 1
