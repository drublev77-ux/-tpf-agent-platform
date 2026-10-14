"""What the platform does on its own between orders: reminds a client whose order waits for them, and tells
search engines when the studio site has changed."""
from __future__ import annotations

import json
import re

import httpx
from fastapi.testclient import TestClient

from tpf.config import Settings, get_settings
from tpf.orchestrator import reminders
from tpf.tools import indexnow


class Recorder:
    def __init__(self) -> None:
        self.client: list[tuple[int, str]] = []

    async def to_client(self, order, text):
        self.client.append((order["id"], text))

    async def to_operator(self, order, text):
        pass


async def _waiting_order(env, days: float, language: str = "ru") -> int:
    """An order whose estimate has waited for the client for `days`."""
    order, _ = await env.orch.create_order(client_name="Анна", client_email="anna@example.com", language=language,
                                           request_text="Нужен лендинг для кофейни: меню, адрес, часы работы")
    await env.drain()
    assert (await env.orch.get_order(order["id"]))["state"] == "awaiting_estimate"
    await _age(env, order["id"], days)
    return order["id"]


async def _age(env, order_id: int, days: float) -> None:
    await env.pool.execute("UPDATE orders SET updated_at = now() - make_interval(secs => $2) WHERE id = $1", order_id, days * 86400)
    await env.pool.execute("UPDATE messages SET created_at = now() - make_interval(secs => $2) WHERE order_id = $1", order_id, days * 86400 + 60)


async def test_a_waiting_order_is_reminded_on_its_days_and_no_more(env):
    env.orch.notifier = sent = Recorder()
    fresh = await _waiting_order(env, 1)
    oid = await _waiting_order(env, 2.1, "en")
    sent.client.clear()

    assert await reminders.remind_due(env.orch) == [oid]                     # one day is too early, two is due
    assert len(sent.client) == 1 and sent.client[0][0] == oid
    assert f"order #{oid}" in sent.client[0][1] and "last reminder" not in sent.client[0][1]
    assert (await env.orch.messages(oid))[-1]["text"] == sent.client[0][1]   # the client sees it on the order page too
    assert await reminders.remind_due(env.orch) == []                        # not again on the next round

    await _age(env, oid, 5.2)                                                # the second day has come…
    assert await reminders.remind_due(env.orch) == []                        # …but the first reminder went out today
    await env.pool.execute("ALTER TABLE events DISABLE TRIGGER events_no_update")
    await env.pool.execute("UPDATE events SET created_at = now() - interval '3 days' WHERE name = 'client_reminder'")
    await env.pool.execute("ALTER TABLE events ENABLE TRIGGER events_no_update")
    assert await reminders.remind_due(env.orch) == [oid]
    assert "last reminder" in sent.client[-1][1]
    await _age(env, oid, 9)
    assert await reminders.remind_due(env.orch) == []                        # two were promised, two were sent
    journal = [e["data"] for e in await env.orch.events(oid) if e["name"] == "client_reminder"]
    assert [d["number"] for d in journal] == [1, 2] and journal[0]["state"] == "awaiting_estimate"
    assert not [e for e in await env.orch.events(fresh) if e["name"] == "client_reminder"]


class Refusing(Recorder):
    """A mail service that refuses every letter, as Brevo does to an address it was not told about."""

    async def to_client(self, order, text):
        raise RuntimeError("brevo 401: unrecognised IP address")


async def test_a_reminder_the_mail_service_refused_is_not_counted_and_is_tried_again(env):
    oid = await _waiting_order(env, 2.1)
    env.orch.notifier = Refusing()
    shown = len(await env.orch.messages(oid))

    assert await reminders.remind_due(env.orch) == []                        # nobody was reminded…
    assert len(await env.orch.messages(oid)) == shown                        # …and the order page does not claim otherwise
    failed = [e["data"] for e in await env.orch.events(oid) if e["name"] == "client_reminder_failed"]
    assert len(failed) == 1 and failed[0]["number"] == 1 and "401" in failed[0]["error"]

    env.orch.notifier = sent = Recorder()
    assert await reminders.remind_due(env.orch) == []                        # not every half hour: the next try is a day later
    await env.pool.execute("ALTER TABLE events DISABLE TRIGGER events_no_update")
    await env.pool.execute("UPDATE events SET created_at = now() - interval '25 hours' WHERE name LIKE 'client_reminder%'")
    await env.pool.execute("ALTER TABLE events ENABLE TRIGGER events_no_update")
    assert await reminders.remind_due(env.orch) == [oid]
    assert "last reminder" not in sent.client[0][1] and "Это последнее" not in sent.client[0][1]   # still the first of two
    assert (await env.orch.messages(oid))[-1]["text"] == sent.client[0][1]
    journal = [e["data"]["number"] for e in await env.orch.events(oid) if e["name"] == "client_reminder"]
    assert journal == [1, 1]


async def test_reminders_leave_alone_what_does_not_wait_for_the_client(env):
    env.orch.notifier = sent = Recorder()
    answered = await _waiting_order(env, 3)
    await env.orch.add_message(answered, "in", "Смету посмотрю в понедельник")       # the client wrote today
    ancient = await _waiting_order(env, 40)                                          # a letter now would come out of nowhere
    moved = await _waiting_order(env, 3)
    await env.pool.execute("UPDATE orders SET state = 'development' WHERE id = $1", moved)   # ours to do, not the client's
    paying = await _waiting_order(env, 3)
    await env.pool.execute("UPDATE orders SET state = 'awaiting_payment' WHERE id = $1", paying)
    sent.client.clear()

    env.orch.settings = env.settings.model_copy(update={"payment_provider": "stripe"})
    assert await reminders.remind_due(env.orch) == []            # Stripe sends a fresh link itself when one expires
    env.orch.settings = env.settings.model_copy(update={"payment_provider": "manual"})
    assert await reminders.remind_due(env.orch) == [paying]
    assert "предоплату" in sent.client[-1][1]
    env.orch.settings = env.settings.model_copy(update={"client_reminder_days": ""})
    await _age(env, answered, 3)
    assert await reminders.remind_due(env.orch) == []            # switched off
    assert Settings(client_reminder_days="5, 2").reminder_days == [2, 5] and Settings(client_reminder_days="soon").reminder_days == []
    assert {answered, ancient, moved} & {i for i, _ in sent.client} == set()


def _engines(live_hash: str, answers: dict[str, int], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=f'<html><head><meta name="tpf-source" content="{live_hash}"></head></html>')
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(answers.get(request.url.host, 200))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_changed_pages_are_reported_once_the_site_serves_them(pool):
    seen: list = []
    async with _engines("0" * 16, {}, seen) as http:                    # the site still serves the previous version
        assert await indexnow.submit(pool, http) == [] and seen == []
    assert indexnow.ENDPOINTS == ("https://api.indexnow.org/indexnow",)     # it shares every report with Yandex and Bing
    async with _engines(indexnow.source_hash(), {"api.indexnow.org": 429}, seen) as http:
        assert await indexnow.submit(pool, http) == []                    # refused: asked again on the next round
    assert [url for url, _ in seen] == list(indexnow.ENDPOINTS)
    body = seen[0][1]
    assert body["host"] == "tpf-technology.com" and body["key"] == indexnow.key()
    assert body["keyLocation"] == f"https://tpf-technology.com/indexnow-{body['key']}.txt"
    assert body["urlList"][0] == "https://tpf-technology.com/" and "https://tpf-technology.com/en" in body["urlList"]
    seen.clear()
    async with _engines(indexnow.source_hash(), {}, seen) as http:
        assert await indexnow.submit(pool, http) == ["https://api.indexnow.org/indexnow"]
        assert await indexnow.submit(pool, http) == []                    # and then nobody, until the site changes
    assert len(seen) == 1
    await pool.execute("UPDATE app_settings SET value = 'an-earlier-version' WHERE key LIKE 'indexnow:%'")
    async with _engines(indexnow.source_hash(), {}, seen) as http:
        assert len(await indexnow.submit(pool, http)) == 1


async def test_a_refused_report_says_why(pool, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=f'<meta name="tpf-source" content="{indexnow.source_hash()}">')
        if not answers:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(302, headers={"location": "https://example.org/showcaptcha"})
    caplog.set_level("WARNING", logger="tpf.indexnow")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        answers: list = []
        assert await indexnow.submit(pool, http) == []
        answers.append("redirect")
        assert await indexnow.submit(pool, http) == []
    lines = [r.getMessage() for r in caplog.records]
    assert any("api.indexnow.org/indexnow answered 0 (RemoteProtocolError: Server disconnected" in line for line in lines)
    assert any("api.indexnow.org/indexnow answered 302 -> https://example.org/showcaptcha" in line for line in lines)
    assert await pool.fetchval("SELECT count(*) FROM app_settings WHERE key LIKE 'indexnow:%'") == 0


def test_the_reported_pages_are_the_sitemap_and_the_key_is_served(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as client:
        sitemap = client.get("/sitemap.xml").text
        assert sorted(re.findall(r"<loc>(.*?)</loc>", sitemap)) == sorted(indexnow.urls())
        proof = client.get(f"/indexnow-{indexnow.key()}.txt")
        assert proof.status_code == 200 and proof.text == indexnow.key()
        page = client.get("/").text
        assert f'name="tpf-source" content="{indexnow.source_hash()}"' in page      # what is_live() looks for
    get_settings.cache_clear()
