"""HTTP API: intake, client auth by order token, state guards, operator auth."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tpf.config import get_settings


@pytest.fixture
def client(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_intake_and_client_access(client):
    r = client.post("/orders", json={
        "client_name": "Li Wei", "client_email": "li@example.com", "language": "zh",
        "request_text": "我们需要一个咖啡店的落地页，包含菜单和地址。",
    })
    assert r.status_code == 201
    oid, token = r.json()["order"]["id"], r.json()["client_token"]
    assert r.json()["order"]["state"] == "spec"

    assert client.get(f"/orders/{oid}", headers={"X-Order-Token": token}).status_code == 200
    assert client.get(f"/orders/{oid}", headers={"X-Order-Token": "wrong"}).status_code == 404

    # Approving an estimate that does not exist yet is refused, not silently accepted.
    r = client.post(f"/orders/{oid}/estimate", headers={"X-Order-Token": token}, json={"approved": True})
    assert r.status_code == 409


def test_operator_endpoints_need_the_admin_token(client):
    assert client.get("/admin/orders", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.get("/admin/orders", headers={"X-Admin-Token": "test-admin"}).status_code == 200


def test_stripe_webhook_requires_a_valid_signature(client, monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    from tpf.config import get_settings
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    get_settings.cache_clear()
    body = json.dumps({"type": "checkout.session.completed",
                       "data": {"object": {"id": "cs_unknown", "payment_status": "paid", "metadata": {"stage": "prepayment"}}}}).encode()
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert r.status_code == 200                               # signed, unknown session: acknowledged, nothing moves
    assert client.get("/pay/done?order=7").status_code == 200


def test_validation_rejects_bad_input(client):
    r = client.post("/orders", json={"client_name": "A", "client_email": "not-an-email", "request_text": "short"})
    assert r.status_code == 422


def test_public_site_and_intake_limit(client, monkeypatch):
    page = client.get("/")
    assert page.status_code == 200 and "TPF" in page.text and "fetch('/orders'" in page.text
    assert client.get("/brand.jpg").headers["content-type"] == "image/jpeg"
    monkeypatch.setenv("INTAKE_HOURLY_LIMIT", "1")
    get_settings.cache_clear()
    order = {"client_name": "A", "client_email": "a@example.com", "request_text": "Нужен лендинг для кофейни."}
    assert client.post("/orders", json=order).status_code == 201
    assert client.post("/orders", json=order).status_code == 429


def test_operator_console(client, monkeypatch):
    import re
    import httpx
    from tpf.api import admin
    from tpf.notifications import EmailNotifier

    for k, v in {"EMAIL_PROVIDER": "brevo", "EMAIL_API_KEY": "k", "EMAIL_FROM": "TPF <a@b.co>", "OPERATOR_EMAIL": "ops@b.co",
                 "PUBLIC_BASE_URL": "https://testserver"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    sent: list[str] = []

    async def fake_send(self, to, subject, text, html):
        sent.append(text)
    monkeypatch.setattr(EmailNotifier, "send", fake_send)

    oid = client.post("/orders", json={"client_name": "A", "client_email": "a@example.com",
                                       "request_text": "Нужен лендинг для кофейни."}).json()["order"]["id"]
    sent.clear()
    assert "Вход оператора" in client.get("/admin").text                      # signed out: no orders shown
    assert client.get(f"/admin/order/{oid}").status_code == 401
    assert client.post(f"/admin/order/{oid}/cancel", data={"sure": "1"}).status_code == 401
    assert "err=link" in client.get("/admin/enter?s=1.bad", follow_redirects=False).headers["location"]

    assert "sent=1" in client.post("/admin/login", follow_redirects=False).headers["location"]
    assert "err=wait" in client.post("/admin/login", follow_redirects=False).headers["location"]   # throttled
    link = re.search(r"https://testserver(/admin/enter\?s=\S+)", sent[0])[1]
    secure = httpx.Client  # TestClient speaks http; the session cookie is Secure, so carry it by hand
    r = client.get(link, follow_redirects=False)
    cookie = r.cookies.get(admin.COOKIE) or re.search(r"tpf_admin=([^;]+)", r.headers["set-cookie"])[1]
    auth = {"Cookie": f"{admin.COOKIE}={cookie}"}
    assert secure and f"№{oid}" in client.get("/admin", headers=auth).text

    page = client.get(f"/admin/order/{oid}", headers=auth).text
    assert "Отменить заказ" in page and "a@example.com" in page
    assert client.post(f"/admin/order/{oid}/cancel", data={"sure": "1"}, headers=auth | {"Origin": "https://evil.example"}).status_code == 403
    r = client.post(f"/admin/order/{oid}/cancel", data={"sure": "1"}, headers=auth, follow_redirects=False)
    assert "done=" in r.headers["location"]
    assert client.get(f"/orders/{oid}", headers={"X-Order-Token": "x"}).status_code == 404
    assert "Отменён" in client.get(f"/admin/order/{oid}", headers=auth).text

    # an order entered by hand (a marketplace or messenger lead)
    assert "Новый заказ вручную" in client.get("/admin", headers=auth).text
    lead = {"client_name": "Li Wei", "client_email": "li@example.com", "language": "zh", "channel": "wechat",
            "request_text": "我们需要一个咖啡店的落地页，包含菜单和地址。"}
    assert client.post("/admin/orders/new", data=lead).status_code == 401
    bad = client.post("/admin/orders/new", data=lead | {"client_email": "nope"}, headers=auth, follow_redirects=False)
    assert bad.headers["location"].startswith("/admin?err=")
    made = client.post("/admin/orders/new", data=lead, headers=auth, follow_redirects=False)
    new_id = int(re.search(r"/admin/order/(\d+)", made.headers["location"])[1])
    page = client.get(f"/admin/order/{new_id}", headers=auth).text
    assert "Li Wei" in page and "zh · wechat" in page and new_id != oid


def test_operator_redeploys_only_a_delivered_order(client):
    from tpf.api.main import app
    admin = {"X-Admin-Token": "test-admin"}
    r = client.post("/orders", json={"client_name": "A", "client_email": "a@example.com", "language": "en", "request_text": "a landing page please"})
    oid = r.json()["order"]["id"]
    assert client.post(f"/admin/orders/{oid}/redeploy", json={"note": "x"}).status_code == 422       # no operator token
    assert client.post(f"/admin/orders/{oid}/redeploy", json={"note": "x"}, headers=admin).status_code == 409   # not delivered
    assert client.post("/admin/orders/999999/redeploy", json={}, headers=admin).status_code == 404
    pool = app.state.orch.pool
    client.portal.call(pool.execute, "UPDATE tasks SET status='done' WHERE order_id=$1", oid)
    client.portal.call(pool.execute, "UPDATE orders SET state='delivered' WHERE id=$1", oid)
    r = client.post(f"/admin/orders/{oid}/redeploy", json={"note": "template fix"}, headers=admin)
    assert r.status_code == 200
    task = client.portal.call(pool.fetchrow, "SELECT kind, payload, status FROM tasks WHERE id=$1", r.json()["task"])
    assert dict(task) == {"kind": "redeploy_production", "payload": {"note": "template fix"}, "status": "queued"}
    assert client.post(f"/admin/orders/{oid}/redeploy", json={}, headers=admin).status_code == 409    # one at a time


# --- an order that comes with a video ----------------------------------------------------------------

def _upload(client, data: bytes, name="task.mp4"):
    from tpf.api import uploads
    up = client.post("/uploads", json={"size": len(data), "filename": name})
    assert up.status_code == 201, up.text
    u = up.json()
    for n in range(u["chunks"]):
        r = client.put(f"/uploads/{u['id']}/chunks/{n}", content=data[n * uploads.CHUNK:(n + 1) * uploads.CHUNK],
                       headers={"x-upload-token": u["token"]})
        assert r.status_code == 200, r.text
    return u, r.json()


def test_video_upload_arrives_in_pieces_and_is_guarded(client, monkeypatch):
    from tpf.api import uploads
    monkeypatch.setattr(uploads, "CHUNK", 1000)
    assert client.post("/uploads", json={"size": 10, "filename": "notes.exe"}).status_code == 415
    assert client.post("/uploads", json={"size": uploads.MAX_SIZE + 1, "filename": "a.mp4"}).status_code == 422
    data = bytes(range(256)) * 10 + b"tail"                      # 2564 bytes: three pieces
    u = client.post("/uploads", json={"size": len(data), "filename": "a.MOV"}).json()
    assert u["chunks"] == 3
    put = lambda n, body, token=u["token"]: client.put(f"/uploads/{u['id']}/chunks/{n}", content=body, headers={"x-upload-token": token})  # noqa: E731
    assert put(0, data[:1000], "wrong").status_code == 404
    assert put(0, data[:999]).status_code == 400 and put(5, b"x").status_code == 409
    assert put(0, data[:1000]).json() == {"received": 1000, "complete": False}
    assert put(0, data[:1000]).json()["received"] == 1000        # a repeated piece changes nothing
    assert put(2, data[2000:]).json()["complete"] is False
    order = {"client_name": "Анна", "client_email": "anna@example.com", "request_text": "[Сайт] Описание — в видео."}
    assert client.post("/orders", json=order | {"video": {"id": u["id"], "token": u["token"]}}).status_code == 422   # not finished
    assert put(1, data[1000:2000]).json() == {"received": len(data), "complete": True}
    assert put(1, data[1000:2000]).status_code == 409            # nothing more is accepted
    assert client.post("/orders", json=order | {"video": {"id": u["id"], "token": "wrong"}}).status_code == 422
    for _ in range(uploads.PER_IP_HOUR - 1):
        assert client.post("/uploads", json={"size": 5, "filename": "b.mp4"}).status_code == 201
    assert client.post("/uploads", json={"size": 5, "filename": "b.mp4"}).status_code == 429


def test_order_with_a_video_waits_for_it_then_reaches_the_analyst(client, monkeypatch):
    import asyncio

    from tpf.agents import video as video_agent
    from tpf.agents.base import Ctx
    from tpf.api.main import app
    from tpf.tools import video as video_tool

    data = b"not really a video" * 50
    u, last = _upload(client, data)
    assert last["complete"]
    r = client.post("/orders", json={"client_name": "Анна", "client_email": "anna@example.com", "language": "ru",
                                     "request_text": "[Сайт] Описание — в приложенном видео.",
                                     "video": {"id": u["id"], "token": u["token"]}})
    assert r.status_code == 201 and r.json()["order"]["state"] == "new"          # the analyst has not started
    oid = r.json()["order"]["id"]
    orch = app.state.orch
    call = client.portal.call
    task = call(orch.pool.fetchrow, "SELECT kind, payload FROM tasks WHERE order_id=$1", oid)
    assert task["kind"] == "understand_video" and task["payload"] == {"upload_id": u["id"]}
    # the same upload cannot be attached to a second order
    assert client.post("/orders", json={"client_name": "X", "client_email": "x@example.com", "request_text": "0123456789",
                                        "video": {"id": u["id"], "token": u["token"]}}).status_code == 422

    seen: dict = {}

    async def fake_watch(path, workdir, language=None):
        seen["bytes"] = path.read_bytes()
        return video_tool.Watched(42.0, frames=[(1.3, b"\xff\xd8a"), (65.0, b"\xff\xd8b")], speech="Нужен сайт для салона красоты с записью")
    monkeypatch.setattr(video_tool, "watch", fake_watch)

    class LLM:
        async def structured(self, **kw):
            seen["llm"] = kw
            return {"summary": "Клиентка просит сайт салона красоты с онлайн-записью.", "shown": "Сайт-пример beauty.example",
                    "details": ["Запись на стрижку", "Телефон +375 29 000-00-00"], "unclear": ["Нужна ли оплата онлайн"]}

    async def go():
        order = await orch.get_order(oid)
        ctx = Ctx(orch, LLM(), orch.settings, None, None, None, order, payload={"upload_id": u["id"]})
        return await video_agent.run(ctx)
    outcome = call(lambda: go())
    assert seen["bytes"] == data and outcome.event == "intake_accepted" and outcome.data == {"video": "processed"}
    assert [i["label"] for i in seen["llm"]["images"]] == ["Frame at 0:01", "Frame at 1:05"]
    assert "салона красоты с записью" in seen["llm"]["prompt"]
    msgs = call(orch.pool.fetch, "SELECT direction, text FROM messages WHERE order_id=$1 ORDER BY id", oid)
    text = msgs[-1]["text"]
    assert msgs[-1]["direction"] == "in" and text.startswith("[Содержание вашего видео") and "Запись на стрижку" in text
    assert "Осталось неясным" in text and "«Нужен сайт для салона" in text
    assert call(orch.pool.fetchval, "SELECT count(*) FROM video_chunks WHERE upload_id=$1", u["id"]) == 0     # the file is gone
    assert call(orch.pool.fetchval, "SELECT status FROM video_uploads WHERE id=$1", u["id"]) == "processed"

    # an unreadable video does not strand the order
    async def broken(path, workdir, language=None):
        raise video_tool.VideoError("not a video")
    monkeypatch.setattr(video_tool, "watch", broken)
    outcome = call(lambda: go())
    assert outcome.event == "intake_accepted" and outcome.data == {"video": "failed"}
    last = call(orch.pool.fetchrow, "SELECT direction, text FROM messages WHERE order_id=$1 ORDER BY id DESC LIMIT 1", oid)
    assert last["direction"] == "out" and "разобрать его не удалось" in last["text"]
