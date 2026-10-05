"""Client payments: Stripe Checkout links, webhook verification, and the payment agent tasks.

Flow (PREPAYMENT_SHARE=0.5): estimate approved -> AWAITING_PAYMENT -> the worker creates a Stripe
Checkout link and sends it -> Stripe calls /webhooks/stripe -> PAYMENT_RECEIVED -> work starts.
After acceptance the same happens for the remaining balance before the production release.
Amounts always come from the approved estimate, computed by code; a model never sets a price.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

import httpx

from tpf.config import Settings

STAGES = ("prepayment", "final")
# Currencies Stripe charges without minor units.
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}


class PaymentError(RuntimeError):
    pass


class WebhookError(ValueError):
    pass


@dataclass
class Checkout:
    session_id: str
    url: str


class PaymentGateway(Protocol):
    provider: str

    async def create_checkout(
        self, *, order_id: int, stage: str, amount: Decimal, currency: str, email: str, description: str,
    ) -> Checkout: ...


def stage_amounts(price: float | Decimal, share: float) -> dict[str, Decimal]:
    """Split a price into prepayment and final amounts; they always add up to the price exactly."""
    total = Decimal(str(price)).quantize(Decimal("0.01"))
    pre = (total * Decimal(str(share))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"prepayment": pre, "final": total - pre}


def to_minor_units(amount: Decimal, currency: str) -> int:
    if currency.lower() in ZERO_DECIMAL:
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class StripeGateway:
    provider = "stripe"
    API = "https://api.stripe.com/v1"

    def __init__(self, settings: Settings) -> None:
        if not settings.stripe_secret_key:
            raise PaymentError("STRIPE_SECRET_KEY is not set")
        self.key = settings.stripe_secret_key
        self.base = settings.public_base_url.rstrip("/")

    async def create_checkout(
        self, *, order_id: int, stage: str, amount: Decimal, currency: str, email: str, description: str,
    ) -> Checkout:
        form = {
            "mode": "payment",
            "customer_email": email,
            "client_reference_id": str(order_id),
            "success_url": f"{self.base}/pay/done?order={order_id}&stage={stage}",
            "cancel_url": f"{self.base}/pay/cancelled?order={order_id}&stage={stage}",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(to_minor_units(amount, currency)),
            "line_items[0][price_data][product_data][name]": description,
            "metadata[order_id]": str(order_id),
            "metadata[stage]": stage,
            "payment_intent_data[metadata][order_id]": str(order_id),
            "payment_intent_data[metadata][stage]": stage,
            # Work starts on payment, so only methods that confirm at once are offered. A SEPA direct debit is
            # confirmed days later and can be reversed by the payer's bank for weeks.
            "excluded_payment_method_types[0]": "sepa_debit",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.API}/checkout/sessions", data=form,
                headers={"Authorization": f"Bearer {self.key}",
                         # Retrying the same task must not create a second live link.
                         "Idempotency-Key": f"tpf-{order_id}-{stage}-{int(time.time() // 3600)}"},
            )
        if r.status_code >= 400:
            raise PaymentError(f"Stripe {r.status_code}: {r.text[:300]}")
        data = r.json()
        return Checkout(data["id"], data["url"])


def verify_stripe_webhook(payload: bytes, signature_header: str | None, secret: str, tolerance: int = 300,
                          now: float | None = None) -> dict[str, Any]:
    """Check the Stripe-Signature header (HMAC-SHA256 over "t.payload") and return the event."""
    if not secret:
        raise WebhookError("STRIPE_WEBHOOK_SECRET is not set")
    if not signature_header:
        raise WebhookError("missing Stripe-Signature header")
    parts = [p.split("=", 1) for p in signature_header.split(",") if "=" in p]
    timestamp = next((v for k, v in parts if k == "t"), None)
    signatures = [v for k, v in parts if k == "v1"]
    if not timestamp or not timestamp.isdigit() or not signatures:
        raise WebhookError("malformed Stripe-Signature header")
    if abs((now or time.time()) - int(timestamp)) > tolerance:
        raise WebhookError("webhook timestamp outside tolerance")
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise WebhookError("signature mismatch")
    return json.loads(payload)


WEBHOOK_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.expired")
SECRET_KEY = "stripe_webhook_secret"
ENDPOINT_KEY = "stripe_webhook_endpoint_id"


async def webhook_secret(settings: Settings, pool: Any) -> str:
    """The signing secret: from the environment if set there, else the one the worker provisioned."""
    if settings.stripe_webhook_secret:
        return settings.stripe_webhook_secret
    return await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", SECRET_KEY) or ""


async def ensure_stripe_webhook(settings: Settings, pool: Any, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Make sure Stripe sends checkout events to this platform, creating the endpoint if needed.

    Stripe reveals an endpoint's signing secret only when it is created, so an endpoint for our URL
    whose secret we do not hold is replaced by a new one (that is the only way to learn the secret).
    Returns what was done, for the log."""
    url = settings.public_base_url.rstrip("/") + "/webhooks/stripe"
    own = client is None
    client = client or httpx.AsyncClient(base_url=StripeGateway.API, timeout=30,
                                         headers={"Authorization": f"Bearer {settings.stripe_secret_key}"})
    try:
        account = (await client.get("/account")).json()
        info: dict[str, Any] = {"account": account.get("id"), "country": account.get("country"),
                                "charges_enabled": account.get("charges_enabled"),
                                "mode": "live" if settings.stripe_secret_key.startswith(("sk_live", "rk_live")) else "test"}
        stored_id = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", ENDPOINT_KEY)
        stored_secret = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", SECRET_KEY)

        r = await client.get("/webhook_endpoints", params={"limit": 100})
        if r.status_code >= 400:
            raise PaymentError(f"Stripe {r.status_code}: {r.text[:300]}")
        endpoints = r.json().get("data", [])
        ours = [e for e in endpoints if e.get("url") == url]
        # The endpoint this platform created earlier under another address (the domain changed) goes too.
        ours += [e for e in endpoints if e["id"] == stored_id and e.get("url") != url]
        keep = next((e for e in ours if e["id"] == stored_id and e.get("url") == url and stored_secret), None)
        if keep and set(WEBHOOK_EVENTS) <= set(keep.get("enabled_events", [])) and keep.get("status") == "enabled":
            extra = [e["id"] for e in ours if e["id"] != keep["id"]]
            for eid in extra:  # leftovers: an old address, or a twin made by a worker that started at the same moment
                await client.delete(f"/webhook_endpoints/{eid}")
            return info | {"webhook": "ok", "endpoint": keep["id"]} | ({"removed": extra} if extra else {})

        for e in ours:  # endpoints for our URL whose secret we cannot know
            await client.delete(f"/webhook_endpoints/{e['id']}")
        form = {"url": url, "description": "TPF Agent Platform: client payments", "enabled_events[]": list(WEBHOOK_EVENTS)}
        created = await client.post("/webhook_endpoints", data=form)
        if created.status_code >= 400:
            raise PaymentError(f"Stripe {created.status_code}: {created.text[:300]}")
        ep = created.json()
        async with pool.acquire() as conn, conn.transaction():
            for key, value in ((SECRET_KEY, ep["secret"]), (ENDPOINT_KEY, ep["id"])):
                await conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", key, value)
        return info | {"webhook": "created", "endpoint": ep["id"], "replaced": [e["id"] for e in ours]}
    finally:
        if own:
            await client.aclose()


def make_gateway(settings: Settings) -> PaymentGateway | None:
    if settings.payment_provider == "stripe":
        return StripeGateway(settings)
    return None


# --- the worker tasks that request a payment ---------------------------------------------------

TEXTS = {
    "prepayment": {
        "ru": "Смета по заказу №{id} утверждена. Чтобы мы начали работу, внесите предоплату {amount}: {url}",
        "en": "The estimate for order #{id} is approved. To start the work, please pay the deposit of {amount}: {url}",
        "zh": "订单 #{id} 的报价已确认。请支付预付款 {amount} 以便开工：{url}",
    },
    "final": {
        "ru": "Работа по заказу №{id} принята. Оплатите оставшиеся {amount}, и мы сразу выпустим проект в работу: {url}",
        "en": "Order #{id} is accepted. Please pay the remaining {amount} and we will release it right away: {url}",
        "zh": "订单 #{id} 已验收。请支付剩余款项 {amount}，我们将立即上线：{url}",
    },
}
MANUAL_TEXTS = {
    "ru": "К оплате по заказу №{id}: {amount}. Счёт с реквизитами пришлём отдельным письмом.",
    "en": "Amount due for order #{id}: {amount}. We will send an invoice with bank details separately.",
    "zh": "订单 #{id} 应付金额：{amount}。我们将另行发送带银行信息的发票。",
}
DESCRIPTIONS = {
    "prepayment": "TPF Technology — order #{id}: deposit",
    "final": "TPF Technology — order #{id}: final payment",
}
REUSE_LINK_SEC = 20 * 3600  # Stripe Checkout links live 24 h; reuse a fresh one instead of making another


async def request_payment(ctx: Any, stage: str) -> Any:
    from tpf.agents.base import Outcome  # local import: agents import this module's siblings

    order = ctx.order
    estimate = await ctx.orch.latest_artifact(order["id"], "estimate")
    if not estimate or not estimate.get("price"):
        return Outcome(escalate="Нет утверждённой сметы для выставления оплаты")
    currency = estimate.get("currency", ctx.settings.currency)
    amount = stage_amounts(estimate["price"], ctx.settings.prepayment_share)[stage]
    if amount <= 0:
        return Outcome(escalate=f"Сумма к оплате ({stage}) равна нулю — проверьте PREPAYMENT_SHARE")
    shown = f"{amount:,.2f} {currency}".replace(",", " ")
    pool = ctx.orch.pool

    already_paid = await pool.fetchval(
        "SELECT 1 FROM payments WHERE order_id = $1 AND stage = $2 AND status = 'paid'", order["id"], stage)
    if already_paid:  # the webhook won the race; nothing to request
        return Outcome()

    gateway: PaymentGateway | None = ctx.payments
    if gateway is None:  # manual: the operator invoices and confirms in /admin
        await pool.execute(
            "INSERT INTO payments (order_id, stage, amount, currency, provider) VALUES ($1,$2,$3,$4,'manual')",
            order["id"], stage, amount, currency)
        await ctx.orch.notify_client(order, MANUAL_TEXTS.get(order["language"], MANUAL_TEXTS["en"]).format(id=order["id"], amount=shown))
        await ctx.orch.notify_operator(order, f"Заказ #{order['id']}: выставите счёт на {shown} ({stage}) и подтвердите оплату в /admin")
        return Outcome()

    fresh = await pool.fetchrow(
        "SELECT url FROM payments WHERE order_id = $1 AND stage = $2 AND status = 'pending' AND url IS NOT NULL "
        "AND created_at > now() - make_interval(secs => $3) ORDER BY id DESC LIMIT 1",
        order["id"], stage, REUSE_LINK_SEC)
    if fresh:
        url = fresh["url"]
    else:
        checkout = await gateway.create_checkout(
            order_id=order["id"], stage=stage, amount=amount, currency=currency,
            email=order["client_email"], description=DESCRIPTIONS[stage].format(id=order["id"]))
        await pool.execute(
            "INSERT INTO payments (order_id, stage, amount, currency, provider, provider_session_id, url) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            order["id"], stage, amount, currency, gateway.provider, checkout.session_id, checkout.url)
        url = checkout.url
    await ctx.orch.log(order["id"], "payment_requested", "payments", {"stage": stage, "amount": str(amount), "currency": currency}, kind="system")
    texts = TEXTS[stage]
    await ctx.orch.notify_client(order, texts.get(order["language"], texts["en"]).format(id=order["id"], amount=shown, url=url))
    return Outcome()


async def request_prepayment(ctx: Any) -> Any:
    return await request_payment(ctx, "prepayment")


async def request_final_payment(ctx: Any) -> Any:
    return await request_payment(ctx, "final")
