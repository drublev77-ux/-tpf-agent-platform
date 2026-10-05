"""Payments: 50/50 Stripe flow, webhook signatures, idempotency, expiry, manual confirmation."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal

import pytest

from tpf.orchestrator.states import Ev, State
from tpf.payments import Checkout, WebhookError, stage_amounts, to_minor_units, verify_stripe_webhook

from test_pipeline import new_order, state


class FakeGateway:
    provider = "stripe"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_checkout(self, **kw) -> Checkout:
        self.calls.append(kw)
        n = len(self.calls)
        return Checkout(f"cs_test_{n}", f"https://checkout.stripe.com/c/pay/cs_test_{n}")


@pytest.fixture
def pay_env(env):
    env.settings.payment_provider = "stripe"
    env.settings.prepayment_share = 0.5
    env.worker.payments = FakeGateway()
    return env


def sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


# --- amounts -----------------------------------------------------------------------------------

def test_split_always_adds_up_to_the_price():
    for price, share in [(640, 0.5), (999.99, 0.5), (100, 0.3), (0.03, 0.5), (1000, 1), (1000, 0)]:
        parts = stage_amounts(price, share)
        assert parts["prepayment"] + parts["final"] == Decimal(str(price)).quantize(Decimal("0.01"))


def test_minor_units():
    assert to_minor_units(Decimal("320.00"), "USD") == 32000
    assert to_minor_units(Decimal("499.995"), "eur") == 50000
    assert to_minor_units(Decimal("5000"), "JPY") == 5000


# --- the whole paid order ----------------------------------------------------------------------

async def test_order_with_50_50_payments(pay_env):
    env = pay_env
    oid, _ = await new_order(env)
    await env.drain()
    estimate = await env.orch.latest_artifact(oid, "estimate")
    assert estimate["payments"] == {"prepayment": 320.0, "final": 320.0}
    first = [m["text"] for m in await env.orch.messages(oid) if m["direction"] == "out"][0]
    assert "320.00 USD до старта" in first and "320.00 USD перед запуском" in first

    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_PAYMENT
    call = env.worker.payments.calls[0]
    assert (call["stage"], call["amount"], call["currency"], call["email"]) == ("prepayment", Decimal("320.00"), "USD", "anna@example.com")
    link_msg = [m["text"] for m in await env.orch.messages(oid) if m["direction"] == "out"][-1]
    assert "https://checkout.stripe.com/c/pay/cs_test_1" in link_msg
    assert env.llm.calls.count("architect") == 0            # no work before the money

    assert await env.orch.record_payment(stage="prepayment", session_id="cs_test_1", actor="stripe")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ACCEPTANCE

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_FINAL_PAYMENT
    assert [d[1] for d in env.deployer.deploys] == [False]  # nothing in production yet
    assert env.worker.payments.calls[1]["stage"] == "final"

    assert await env.orch.record_payment(stage="final", session_id="cs_test_2", actor="stripe")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED and [d[1] for d in env.deployer.deploys] == [False, True]
    paid = await env.pool.fetch("SELECT stage, amount FROM payments WHERE order_id=$1 AND status='paid' ORDER BY id", oid)
    assert [(r["stage"], r["amount"]) for r in paid] == [("prepayment", Decimal("320.00")), ("final", Decimal("320.00"))]


async def test_duplicate_webhook_counts_once(pay_env):
    env = pay_env
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert await env.orch.record_payment(stage="prepayment", session_id="cs_test_1", actor="stripe")
    assert not await env.orch.record_payment(stage="prepayment", session_id="cs_test_1", actor="stripe")
    transitions = [e["name"] for e in await env.orch.events(oid) if e["kind"] == "transition"]
    assert transitions.count("payment_received") == 1


async def test_retried_request_reuses_the_open_link(pay_env):
    env = pay_env
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    from tpf.agents.base import Ctx
    from tpf.payments import request_prepayment
    ctx = Ctx(env.orch, env.llm, env.settings, env.sandbox, env.deployer, None, await env.orch.get_order(oid),
              payments=env.worker.payments)
    await request_prepayment(ctx)
    assert len(env.worker.payments.calls) == 1               # same link sent again, no second session


async def test_manual_payments_are_confirmed_by_the_operator(env):
    env.settings.payment_provider = "manual"
    env.settings.prepayment_share = 1
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_PAYMENT
    row = await env.pool.fetchrow("SELECT provider, amount, url FROM payments WHERE order_id=$1", oid)
    assert (row["provider"], row["amount"], row["url"]) == ("manual", Decimal("640.00"), None)
    assert await env.orch.record_payment(stage="prepayment", order_id=oid, actor="operator")
    await env.drain()
    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()                                        # share = 1: no final payment
    assert await state(env, oid) is State.DELIVERED


# --- webhook signature -------------------------------------------------------------------------

def test_webhook_signature():
    body = json.dumps({"type": "checkout.session.completed"}).encode()
    assert verify_stripe_webhook(body, sign(body, "whsec_x"), "whsec_x")["type"] == "checkout.session.completed"
    with pytest.raises(WebhookError):
        verify_stripe_webhook(body, sign(body, "whsec_other"), "whsec_x")
    with pytest.raises(WebhookError):
        verify_stripe_webhook(body + b" ", sign(body, "whsec_x"), "whsec_x")        # tampered body
    with pytest.raises(WebhookError):
        verify_stripe_webhook(body, sign(body, "whsec_x", ts=int(time.time()) - 3600), "whsec_x")  # replay
    with pytest.raises(WebhookError):
        verify_stripe_webhook(body, None, "whsec_x")


# --- webhook provisioning ----------------------------------------------------------------------

async def test_worker_provisions_the_stripe_webhook_once(env):
    import httpx
    from tpf.payments import ensure_stripe_webhook, webhook_secret

    env.settings.stripe_secret_key = "sk_test_123"
    env.settings.public_base_url = "https://api.example.com"
    endpoints: dict[str, dict] = {"we_old": {"id": "we_old", "url": "https://api.example.com/webhooks/stripe",
                                            "enabled_events": ["*"], "status": "enabled"}}
    deleted: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/account":
            return httpx.Response(200, json={"id": "acct_1", "country": "EE", "charges_enabled": True})
        if req.method == "GET" and req.url.path == "/v1/webhook_endpoints":
            return httpx.Response(200, json={"data": list(endpoints.values())})
        if req.method == "DELETE":
            eid = req.url.path.rsplit("/", 1)[-1]
            deleted.append(eid)
            endpoints.pop(eid, None)
            return httpx.Response(200, json={"deleted": True})
        if req.method == "POST" and req.url.path == "/v1/webhook_endpoints":
            form = httpx.QueryParams(req.content.decode())
            ep = {"id": "we_new", "url": form["url"], "enabled_events": form.get_list("enabled_events[]"),
                  "status": "enabled", "secret": "whsec_new"}
            endpoints["we_new"] = {k: v for k, v in ep.items() if k != "secret"}
            return httpx.Response(200, json=ep)
        return httpx.Response(404)

    client = httpx.AsyncClient(base_url="https://api.stripe.com/v1", transport=httpx.MockTransport(handler))
    first = await ensure_stripe_webhook(env.settings, env.pool, client)
    assert first["webhook"] == "created" and deleted == ["we_old"] and first["mode"] == "test"
    assert await webhook_secret(env.settings, env.pool) == "whsec_new"

    second = await ensure_stripe_webhook(env.settings, env.pool, client)   # restart: nothing to do
    assert second["webhook"] == "ok" and deleted == ["we_old"]


async def test_webhook_moves_with_the_domain(env):
    import httpx
    from tpf.payments import ensure_stripe_webhook, webhook_secret

    env.settings.stripe_secret_key = "sk_test_123"
    endpoints: dict[str, dict] = {"we_other": {"id": "we_other", "url": "https://someone-else.example/hook", "enabled_events": ["*"], "status": "enabled"}}
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/account":
            return httpx.Response(200, json={"id": "acct_1"})
        if req.method == "GET":
            return httpx.Response(200, json={"data": list(endpoints.values())})
        if req.method == "DELETE":
            endpoints.pop(req.url.path.rsplit("/", 1)[-1], None)
            return httpx.Response(200, json={"deleted": True})
        n["i"] += 1
        form = httpx.QueryParams(req.content.decode())
        ep = {"id": f"we_{n['i']}", "url": form["url"], "enabled_events": form.get_list("enabled_events[]"), "status": "enabled"}
        endpoints[ep["id"]] = ep
        return httpx.Response(200, json=ep | {"secret": f"whsec_{n['i']}"})

    client = httpx.AsyncClient(base_url="https://api.stripe.com/v1", transport=httpx.MockTransport(handler))
    env.settings.public_base_url = "https://old.example.com"
    await ensure_stripe_webhook(env.settings, env.pool, client)
    env.settings.public_base_url = "https://new.example.com"
    moved = await ensure_stripe_webhook(env.settings, env.pool, client)
    assert moved["webhook"] == "created" and moved["replaced"] == ["we_1"]
    assert sorted(endpoints) == ["we_2", "we_other"]                      # ours moved; a stranger's endpoint is untouched
    assert endpoints["we_2"]["url"] == "https://new.example.com/webhooks/stripe"
    assert await webhook_secret(env.settings, env.pool) == "whsec_2"
    assert (await ensure_stripe_webhook(env.settings, env.pool, client))["webhook"] == "ok"


async def test_client_can_be_invoiced_in_dollars(env):
    from tpf.config import Settings

    s = Settings(database_url="postgresql://x/y")
    assert [s.rate_in("USD", k) for k in ("site", "app", "bot", "shop")] == [45, 70, 50, 37]       # the dollar rates of before
    assert [s.rate_in("USD", k, "ru") for k in ("site", "app", "bot", "shop")] == [36, 56, 34, 29]
    assert s.rate_in("EUR", "site") == 40

    env.settings.currency, env.settings.usd_per_eur = "EUR", 1.117
    order, _ = await env.orch.create_order(client_name="Ann", client_email="ann@example.com", language="en",
                                           request_text="A landing page for a bakery, please", currency="USD")
    await env.drain()
    estimate = await env.orch.latest_artifact(order["id"], "estimate")
    assert estimate["currency"] == "USD" and estimate["hourly_rate"] == 45 and estimate["price"] == estimate["total_hours"] * 45
    other, _ = await env.orch.create_order(client_name="Ann", client_email="ann@example.com", language="en",
                                           request_text="A landing page for a bakery, please")
    await env.drain()
    assert (await env.orch.latest_artifact(other["id"], "estimate"))["currency"] == "EUR"
