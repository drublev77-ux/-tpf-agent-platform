"""What a delivered application asks of the platform: e-mail, the AI model, scheduled work and the keys of
outside services its owner enters on the order page."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tpf.api.apps import app_key
from tpf.config import get_settings
from tpf.tools import ticker
from tpf.tools.template import all_files, capabilities_of, capability_rules, platform_files, validate_path

SECRET = "s" * 64


@pytest.fixture
def client(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    for k, v in {"EMAIL_PROVIDER": "brevo", "EMAIL_API_KEY": "k", "EMAIL_FROM": "TPF <a@b.co>", "ANTHROPIC_API_KEY": "sk-test"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _app(client, capabilities, state="delivered", arch=None):
    """An application order that is live, with the given capabilities in its spec."""
    from tpf.api.main import app
    orch = app.state.orch
    r = client.post("/orders", json={"client_name": "Анна", "client_email": "owner@example.com", "language": "ru",
                                     "request_text": "Нужно приложение для учёта заявок с напоминаниями."}).json()
    oid = r["order"]["id"]
    call = client.portal.call
    call(orch.pool.execute, "UPDATE tasks SET status='done' WHERE order_id=$1", oid)
    call(orch.save_artifact, oid, "spec", {"product_type": "app", "title": "Заявки", "capabilities": capabilities}, "analyst")
    if arch is not None:
        call(orch.save_artifact, oid, "architecture", arch, "architect")
    call(orch.pool.execute, "INSERT INTO app_databases (order_id, env, role_name, db_password, session_secret, admin_password) "
                            "VALUES ($1, 'prod', $2, 'ab', $3, 'pw')", oid, f"o{oid}_prod", SECRET)
    call(orch.pool.execute, "UPDATE orders SET state=$2, production_url='https://app.example' WHERE id=$1", oid, state)
    return oid, r["client_token"], orch


def test_new_capabilities_are_part_of_the_application_template():
    plain = {"product_type": "app", "capabilities": ["chat", "email", "documents", "ai", "schedule", "integrations", "nonsense"]}
    assert capabilities_of(plain) == ["documents", "email", "schedule", "ai", "integrations"]       # chat needs people who sign in
    assert capabilities_of(plain | {"user_roles": [{"name": "worker"}]})[0] == "chat"
    assert capabilities_of(plain | {"product_type": "shop"}) == []
    rules = capability_rules(plain)
    for word in ("buildPdf", "parseXlsx", "sendMail", "onTick", "askAI", "serviceKey"):
        assert word in rules
    files = platform_files("https://tpf.example/forms/7", "app")
    for path in ("tpf/pdf.ts", "tpf/pdf-font.ts", "tpf/sheets.ts", "tpf/mail.ts", "tpf/ai.ts", "tpf/services.ts", "tpf/platform.ts"):
        assert path in files
    assert "pdf-lib" in json.loads(files["package.json"])["dependencies"]
    assert "pdf-lib" not in json.loads(platform_files("", "shop")["package.json"])["dependencies"]
    # the route that wakes scheduled work imports the application's own lib/schedule.ts: it ships only with one
    paths = lambda code: {f["path"] for f in all_files(code, "", "app")}        # noqa: E731
    assert "app/api/tpf-tick/route.ts" not in paths([{"path": "app/page.tsx", "content": ""}])
    assert "app/api/tpf-tick/route.ts" in paths([{"path": "lib/schedule.ts", "content": ""}])
    assert validate_path("app/api/tpf-tick/route.ts", "app") and validate_path("lib/schedule.ts", "app") is None


def test_application_mail_is_signed_limited_and_sent_in_the_applications_name(client, monkeypatch):
    from tpf.notifications import EmailNotifier
    sent: list[dict] = []

    async def fake_send(self, to, subject, text, html, *, sender_name="", reply_to=""):
        sent.append({"to": to, "subject": subject, "text": text, "name": sender_name, "reply": reply_to})
    monkeypatch.setattr(EmailNotifier, "send", fake_send)
    oid, _, orch = _app(client, ["email"])
    sent.clear()                                                        # the platform's own letter about the new order
    key = {"x-tpf-app": app_key(SECRET)}
    mail = {"to": "petr@example.com", "subject": "Напоминание", "text": "Завтра в 10:00"}

    assert client.post(f"/apps/{oid}/mail", json=mail).status_code == 401                       # no key
    assert client.post(f"/apps/{oid}/mail", json=mail, headers={"x-tpf-app": app_key("other")}).status_code == 401
    assert client.post(f"/apps/{oid}/mail", json=mail | {"to": "not an address"}, headers=key).json() == {"ok": False, "error": "address"}
    assert client.post(f"/apps/{oid}/mail", json=mail, headers=key).json() == {"ok": True}
    assert sent == [{"to": "petr@example.com", "subject": "Напоминание", "name": "Заявки", "reply": "owner@example.com",
                     "text": sent[0]["text"]}] and sent[0]["text"].startswith("Завтра в 10:00") and "«Заявки»" in sent[0]["text"]
    for _ in range(9):
        assert client.post(f"/apps/{oid}/mail", json=mail, headers=key).status_code == 200
    assert client.post(f"/apps/{oid}/mail", json=mail, headers=key).json() == {"ok": False, "error": "limit"}   # 10 a day to one address
    assert len(sent) == 10
    assert client.post(f"/apps/{oid}/mail", json=mail | {"to": "olga@example.com"}, headers=key).status_code == 200

    # an application whose spec has no e-mail cannot send, and neither can one that is not live
    other, _, _ = _app(client, ["documents"])
    assert client.post(f"/apps/{other}/mail", json=mail, headers=key).json() == {"ok": False, "error": "off"}
    client.portal.call(orch.pool.execute, "UPDATE orders SET state='cancelled' WHERE id=$1", oid)
    assert client.post(f"/apps/{oid}/mail", json=mail, headers=key).status_code == 403


def test_application_ai_is_metered_and_stops_at_the_monthly_allowance(client, monkeypatch):
    import tpf.api.apps as apps
    asked: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        assert request.headers["x-api-key"] == "sk-test"
        content = ([{"type": "tool_use", "name": "answer", "input": {"total": 45.5}}] if body.get("tools")
                   else [{"type": "text", "text": "Готово"}])
        return httpx.Response(200, json={"content": content, "usage": {"input_tokens": 900, "output_tokens": 100}})
    real = httpx.AsyncClient
    monkeypatch.setattr(apps.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), base_url=kw["base_url"]))
    oid, _, orch = _app(client, ["ai"])
    key = {"x-tpf-app": app_key(SECRET)}
    schema = {"type": "object", "properties": {"total": {"type": "number"}}, "required": ["total"]}
    photo = {"type": "image/jpeg", "data": "/9j/4AAQSkZJRg=="}

    assert client.post(f"/apps/{oid}/ai", json={"prompt": "Сумма?"}).status_code == 401
    r = client.post(f"/apps/{oid}/ai", json={"prompt": "Сумма?", "files": [photo], "json": schema}, headers=key).json()
    assert r == {"ok": True, "text": "", "data": {"total": 45.5}}
    sent = asked[0]["messages"][0]["content"]
    assert sent[0]["type"] == "image" and sent[1] == {"type": "text", "text": "Сумма?"} and "data to work" in asked[0]["system"]
    assert client.post(f"/apps/{oid}/ai", json={"prompt": "Привет"}, headers=key).json() == {"ok": True, "text": "Готово", "data": None}
    assert client.post(f"/apps/{oid}/ai", json={"prompt": "x", "files": [{"type": "text/html", "data": "aGk="}]}, headers=key).status_code == 400
    assert client.portal.call(orch.pool.fetchval, "SELECT sum(input_tokens + output_tokens) FROM app_ai_usage WHERE order_id=$1", oid) == 2000

    client.portal.call(orch.pool.execute, "INSERT INTO app_ai_usage (order_id, env, input_tokens, output_tokens) VALUES ($1, 'prod', $2, 0)",
                       oid, get_settings().app_ai_monthly_tokens)
    assert client.post(f"/apps/{oid}/ai", json={"prompt": "Ещё"}, headers=key).json() == {"ok": False, "error": "limit"}
    assert len(asked) == 2                                              # the model was not asked past the allowance


ARCH = {"services": [{"id": "crm", "name": "amoCRM", "base_url": "https://example.amocrm.ru", "purpose": "a lead per request",
                      "keys": [{"id": "crm_token", "label": "Долгосрочный токен", "where": "Настройки → Интеграции"},
                               {"id": "crm_domain", "label": "Адрес аккаунта", "where": "В адресной строке"}]}]}


def test_owner_enters_service_keys_on_the_order_page_and_only_the_ones_the_design_asks_for(client):
    oid, token, orch = _app(client, ["integrations"], arch=ARCH)
    page = client.get(f"/portal/{oid}", params={"t": token}).text
    assert "Внешние сервисы" in page and "amoCRM — Долгосрочный токен" in page and 'name="key_crm_token"' in page and "не введён" in page

    saved = client.post(f"/portal/{oid}/service-keys", follow_redirects=False,
                        data={"t": token, "key_crm_token": " secret-1 ", "key_crm_domain": "", "key_admin_password": "x"})
    assert saved.status_code == 303 and "done=svc" in saved.headers["location"]
    rows = client.portal.call(orch.pool.fetch, "SELECT key_id, value FROM service_keys WHERE order_id=$1", oid)
    assert [(r["key_id"], r["value"]) for r in rows] == [("crm_token", "secret-1")]
    assert client.portal.call(orch.pool.fetchval, "SELECT kind FROM tasks WHERE order_id=$1 AND status='queued'", oid) == "connect_services"
    page = client.get(f"/portal/{oid}", params={"t": token}).text
    assert "secret-1" not in page and "введён" in page                  # a key is never shown again
    assert "err=empty" in client.post(f"/portal/{oid}/service-keys", data={"t": token}, follow_redirects=False).headers["location"]
    assert client.post(f"/portal/{oid}/service-keys", data={"t": "wrong", "key_crm_token": "x"}, follow_redirects=False).status_code == 404

    # without the capability the block is not there, whatever the design says
    plain, plain_token, _ = _app(client, ["email"], arch=ARCH)
    assert "Внешние сервисы" not in client.get(f"/portal/{plain}", params={"t": plain_token}).text


async def test_scheduled_work_wakes_each_due_application_once(pool, settings):
    from tpf.orchestrator.service import Orchestrator
    from tpf.notifications import LogNotifier
    orch = Orchestrator(pool, settings, LogNotifier())
    ids = []
    for caps in (["schedule", "email"], ["email"]):
        order, _ = await orch.create_order(client_name="А", client_email="a@example.com", request_text="Приложение с напоминаниями", language="ru")
        await orch.save_artifact(order["id"], "spec", {"product_type": "app", "capabilities": caps}, "analyst")
        await pool.execute("INSERT INTO app_databases (order_id, env, role_name, db_password, session_secret, admin_password) "
                           "VALUES ($1, 'prod', $2, 'ab', $3, 'pw')", order["id"], f"o{order['id']}_prod", SECRET)
        await pool.execute("UPDATE orders SET state='delivered', production_url=$2 WHERE id=$1", order["id"], f"https://app{order['id']}.example/")
        ids.append(order["id"])
    calls: list[tuple[str, str]] = []
    status = [200]

    def app(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("x-tpf-app", "")))
        return httpx.Response(status[0], json={"ok": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(app)) as http:
        assert await ticker.tick_due(pool, settings, http) == 1
        assert calls == [(f"https://app{ids[0]}.example/api/tpf-tick", app_key(SECRET))]       # only the one with scheduled work
        assert await ticker.tick_due(pool, settings, http) == 0 and len(calls) == 1             # not again before its time
        await pool.execute("UPDATE app_ticks SET last_at = now() - interval '16 minutes'")
        status[0] = 500
        assert await ticker.tick_due(pool, settings, http) == 1
        assert await pool.fetchval("SELECT failures FROM app_ticks WHERE order_id=$1", ids[0]) == 1
        await pool.execute("UPDATE app_ticks SET last_at = now() - interval '16 minutes'")
        assert await ticker.tick_due(pool, settings, http) == 0                                  # a failing one waits longer


def test_without_the_providers_key_the_web_side_asks_through_a_worker(client, monkeypatch):
    import asyncio
    import tpf.api.apps as apps
    from tpf.tools import ai_relay
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    get_settings.cache_clear()
    assert not get_settings().anthropic_api_key
    oid, _, orch = _app(client, ["ai"])
    seen: list[dict] = []

    async def model(settings, call):                     # what the worker, which holds the key, gets from the provider
        seen.append(call)
        return {"content": [{"type": "text", "text": "Ответ"}], "usage": {"input_tokens": 50, "output_tokens": 5}}
    monkeypatch.setattr(ai_relay, "ask_model", model)

    async def worker():
        for _ in range(100):
            if await ai_relay.answer_one(orch.pool, get_settings()):
                return
            await asyncio.sleep(0.05)
    waiting = client.portal.start_task_soon(worker)
    r = client.post(f"/apps/{oid}/ai", json={"prompt": "Вопрос"}, headers={"x-tpf-app": app_key(SECRET)})
    waiting.result(timeout=10)
    assert r.json() == {"ok": True, "text": "Ответ", "data": None}
    assert seen[0]["messages"][0]["content"] == [{"type": "text", "text": "Вопрос"}]
    assert client.portal.call(orch.pool.fetchval, "SELECT count(*) FROM app_ai_requests") == 0          # nothing is kept
    assert client.portal.call(orch.pool.fetchval, "SELECT sum(input_tokens) FROM app_ai_usage WHERE order_id=$1", oid) == 50

    # no worker answers: the application is told so within the wait, not left hanging
    monkeypatch.setattr(apps, "WORKER_WAIT_SEC", 0.8)
    assert client.post(f"/apps/{oid}/ai", json={"prompt": "Вопрос"}, headers={"x-tpf-app": app_key(SECRET)}).json() == {"ok": False, "error": "failed"}
    assert client.portal.call(orch.pool.fetchval, "SELECT count(*) FROM app_ai_requests") == 0


def test_the_reviewer_knows_the_order_page_webhook_is_free_as_the_analyst_does():
    # order 19: the analyst mentioned the built-in webhook, the reviewer took it for an undeclared outside service
    from tpf.agents import analyst
    assert "n8n" in analyst.SCOPE_AUTOMATION and "no work in the estimate" in analyst.SCOPE_AUTOMATION
    webhook = [c for c in analyst.REVIEW_CRITERIA if "n8n" in c]
    assert webhook and "without 'integrations'" in webhook[0] and "without estimate hours" in webhook[0]
