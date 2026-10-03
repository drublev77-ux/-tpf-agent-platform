"""Client portal pages and actions; e-mail notifications through Resend / Brevo."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tpf.config import Settings, get_settings
from tpf.notifications import EmailNotifier, email_html, make_notifier, portal_url


@pytest.fixture
def client(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _order(client, language="ru"):
    r = client.post("/orders", json={
        "client_name": "Анна", "client_email": "anna@example.com", "language": language,
        "request_text": "Нужен лендинг для кофейни: меню, адрес, форма брони столика.",
    })
    return r.json()["order"]["id"], r.json()["client_token"]


def test_portal_page_is_private_and_localized(client):
    oid, token = _order(client, "en")
    page = client.get(f"/portal/{oid}", params={"t": token})
    assert page.status_code == 200 and f"Order #{oid}" in page.text and "<script" not in page.text
    assert client.get(f"/portal/{oid}", params={"t": "wrong"}).status_code == 404
    assert client.get(f"/portal/{oid}").status_code == 404
    assert client.get("/portal/999999", params={"t": token}).status_code == 404


def test_portal_actions(client, pool):
    oid, token = _order(client)
    r = client.post(f"/portal/{oid}/message", data={"t": token, "text": "Добавьте тёмную тему"}, follow_redirects=False)
    assert r.status_code == 303 and "done=message" in r.headers["location"]
    assert "Добавьте тёмную тему" in client.get(f"/portal/{oid}", params={"t": token}).text

    # forged token on an action: same 404 as a missing order
    assert client.post(f"/portal/{oid}/estimate", data={"t": "x", "approved": "1"}).status_code == 404

    # estimate not ready yet → the state machine refuses, the client sees an error, nothing moves
    r = client.post(f"/portal/{oid}/estimate", data={"t": token, "approved": "1"}, follow_redirects=False)
    assert "err=state" in r.headers["location"]

    from tpf.api.main import app
    orch = app.state.orch
    # fast-forward: the analyst has finished (its task is done) and the estimate waits for the client
    client.portal.call(orch.pool.execute, "UPDATE tasks SET status='done' WHERE order_id=$1", oid)
    client.portal.call(orch.pool.execute, "UPDATE orders SET state='awaiting_estimate' WHERE id=$1", oid)

    r = client.post(f"/portal/{oid}/estimate", data={"t": token, "approved": "0"}, follow_redirects=False)
    assert "err=comment" in r.headers["location"]                     # rejecting needs a reason
    r = client.post(f"/portal/{oid}/estimate", data={"t": token, "approved": "0", "comment": "Дешевле"},
                    follow_redirects=False)
    assert "done=rejected" in r.headers["location"]
    state = client.portal.call(orch.pool.fetchval, "SELECT state FROM orders WHERE id=$1", oid)
    assert state != "awaiting_estimate"


# --- e-mail ------------------------------------------------------------------------------------

def _settings(**kw) -> Settings:
    base = dict(email_provider="resend", email_api_key="re_test", email_from="TPF Technology <orders@tpf.tech>",
                email_reply_to="hello@tpf.tech", operator_email="ops@tpf.tech", public_base_url="https://api.tpf.tech")
    base.update(kw)
    return Settings(**base)


ORDER = {"id": 42, "client_email": "anna@example.com", "client_name": "Анна", "client_token": "tok/+=",
         "language": "ru", "channel": "site", "request_text": "..."}


def _capture():
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={"id": "msg_1"})
    return sent, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_resend_client_email_links_the_portal():
    sent, http = _capture()
    await EmailNotifier(_settings(), http).to_client(ORDER, "Смета готова: 640 USD <b>")
    req = sent[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.resend.com/emails" and req.headers["authorization"] == "Bearer re_test"
    assert body["to"] == ["anna@example.com"] and body["subject"] == "TPF Technology — заказ №42"
    link = portal_url("https://api.tpf.tech", 42, "tok/+=")
    assert link == "https://api.tpf.tech/portal/42?t=tok%2F%2B%3D"
    assert link in body["text"] and link in body["html"] and body["reply_to"] == "hello@tpf.tech"
    assert "&lt;b&gt;" in body["html"] and "<b>" not in body["html"]  # client text is escaped


async def test_brevo_and_operator_email():
    sent, http = _capture()
    n = EmailNotifier(_settings(email_provider="brevo", email_api_key="xkeysib"), http)
    await n.to_operator(ORDER, "Заказ на паузе")
    body = json.loads(sent[0].content)
    assert str(sent[0].url) == "https://api.brevo.com/v3/smtp/email" and sent[0].headers["api-key"] == "xkeysib"
    assert body["sender"] == {"email": "orders@tpf.tech", "name": "TPF Technology"}
    assert body["to"] == [{"email": "ops@tpf.tech"}]


async def test_provider_errors_surface():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "domain not verified"})))
    with pytest.raises(RuntimeError, match="domain not verified"):
        await EmailNotifier(_settings(), http).to_client(ORDER, "hi")


def test_notifier_selection_and_links():
    assert isinstance(make_notifier(_settings()), EmailNotifier)
    assert not isinstance(make_notifier(_settings(email_api_key="")), EmailNotifier)
    assert not isinstance(make_notifier(_settings(email_provider="off")), EmailNotifier)
    html = email_html("Смотрите https://x.vercel.app\nспасибо", "zh", "https://p/1?t=a")
    assert '<a href="https://x.vercel.app"' in html and "查看订单" in html and "<br>" in html


# --- source code handover ------------------------------------------------------------------------

class FakeGitHub:
    owner = "acme"

    def __init__(self) -> None:
        self.invited: list[tuple[str, str]] = []

    async def publish(self, repo, files, message):
        return f"https://github.com/{self.owner}/{repo}"

    async def invite(self, repo, username):
        if username == "ghost-user":
            raise LookupError(username)
        self.invited.append((repo, username))
        return "invited"


async def test_client_gets_repo_access_without_moving_the_order(env):
    from tpf.orchestrator import queue
    from tpf.orchestrator.states import Ev, State
    from test_pipeline import new_order, state

    env.worker.github = FakeGitHub()
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.DELIVERED

    for user in ("anna-dev", "ghost-user"):
        async with env.pool.acquire() as conn:
            await queue.enqueue(conn, oid, "grant_repo_access", {"username": user})
        await env.drain()
    assert env.worker.github.invited == [(f"tpf-order-{oid}", "anna-dev")]
    assert await state(env, oid) is State.DELIVERED
    out = [m["text"] for m in await env.orch.messages(oid) if m["direction"] == "out"]
    assert "anna-dev" in out[-2] and f"github.com/acme/tpf-order-{oid}" in out[-2]
    assert "ghost-user" in out[-1] and "Не нашли" in out[-1]
    assert await env.pool.fetchval("SELECT count(*) FROM tasks WHERE order_id=$1 AND status IN ('queued','running')", oid) == 0


def test_portal_source_zip_and_repo_request(client):
    import io, zipfile
    from tpf.api.main import app
    oid, token = _order(client)
    orch = app.state.orch
    assert client.get(f"/portal/{oid}/source.zip", params={"t": token}).status_code == 404     # nothing delivered yet
    client.portal.call(orch.pool.execute, "UPDATE tasks SET status='done' WHERE order_id=$1", oid)
    client.portal.call(orch.save_artifact, oid, "code", {"files": [{"path": "app/page.tsx", "content": "export default 1"}]}, "developer")
    client.portal.call(orch.pool.execute, "UPDATE orders SET state='delivered' WHERE id=$1", oid)
    client.portal.call(orch.log, oid, "code_published", "devops", {"repo": "x"})

    page = client.get(f"/portal/{oid}", params={"t": token}).text
    assert "source.zip" in page and 'name="username"' in page
    assert client.get(f"/portal/{oid}/source.zip", params={"t": "wrong"}).status_code == 404
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/portal/{oid}/source.zip", params={"t": token}).content))
    assert f"tpf-order-{oid}/app/page.tsx" in z.namelist() and f"tpf-order-{oid}/package.json" in z.namelist()

    bad = client.post(f"/portal/{oid}/repo-access", data={"t": token, "username": "not a name!"}, follow_redirects=False)
    assert "err=gh" in bad.headers["location"]
    ok = client.post(f"/portal/{oid}/repo-access", data={"t": token, "username": "@anna-dev"}, follow_redirects=False)
    assert "done=repo" in ok.headers["location"]
    task = client.portal.call(orch.pool.fetchrow, "SELECT kind, payload FROM tasks WHERE order_id=$1 AND status='queued'", oid)
    assert (task["kind"], task["payload"]) == ("grant_repo_access", {"username": "anna-dev"})
    again = client.post(f"/portal/{oid}/repo-access", data={"t": token, "username": "anna-dev"}, follow_redirects=False)
    assert "err=busy" in again.headers["location"]
