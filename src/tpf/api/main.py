"""HTTP API: order intake, client decisions, live status, operator controls."""
from __future__ import annotations

import asyncio
import hmac
from datetime import datetime, timezone
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
import asyncpg
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, Response
from pydantic import BaseModel, EmailStr, Field

from tpf.notifications import portal_url
from tpf.api import apps, uploads
from tpf.config import get_settings
from tpf.db import create_pool
from tpf.notifications import make_notifier
from tpf.orchestrator.service import Orchestrator
from tpf.orchestrator import queue
from tpf.orchestrator.states import TASK_FOR_STATE, Ev, State, TransitionError
from tpf.payments import WebhookError, verify_stripe_webhook, webhook_secret


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Serverless: many short-lived instances, each keeps only a couple of connections.
    app.state.pool = await create_pool(min_size=0, max_size=3)
    app.state.orch = Orchestrator(app.state.pool, settings, make_notifier(settings))
    yield
    await app.state.pool.close()


app = FastAPI(title="TPF Agent Platform", version="0.1.0", lifespan=lifespan)


def orch(request: Request) -> Orchestrator:
    return request.app.state.orch


# --- models ---------------------------------------------------------------------------------

class VideoRef(BaseModel):
    id: int
    token: str = Field(max_length=100)


class OrderIn(BaseModel):
    client_name: str = Field(min_length=1, max_length=200)
    client_email: EmailStr
    request_text: str = Field(min_length=10, max_length=20_000)
    channel: Literal["site", "telegram", "whatsapp", "wechat", "email", "crm"] = "site"
    language: Literal["ru", "en", "zh"] = "ru"
    currency: Literal["EUR", "USD"] | None = None   # the client's choice on the form; empty: the studio's default
    video: VideoRef | None = None      # a finished upload (see uploads.py) to watch before the analyst starts


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class DecisionIn(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=20_000)


class OperatorNote(BaseModel):
    note: str = Field(default="", max_length=20_000)


PUBLIC_FIELDS = ("id", "state", "client_name", "language", "staging_url", "production_url", "created_at", "updated_at")


def public(order: dict[str, Any]) -> dict[str, Any]:
    return {k: order[k] for k in PUBLIC_FIELDS}


# --- auth -----------------------------------------------------------------------------------

async def client_order(order_id: int, request: Request, x_order_token: str = Header(...)) -> dict[str, Any]:
    try:
        order = await orch(request).get_order(order_id)
    except KeyError:
        raise HTTPException(404, "order not found") from None
    if not hmac.compare_digest(order["client_token"], x_order_token):
        raise HTTPException(404, "order not found")  # same answer as a missing order
    return order


def operator(x_admin_token: str = Header(...)) -> None:
    expected = get_settings().admin_token
    if expected == "change-me" or not hmac.compare_digest(expected, x_admin_token):
        raise HTTPException(401, "operator token required")


async def _fire(o: Orchestrator, order_id: int, event: Ev, actor: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        await o.fire(order_id, event, actor=actor, data=data or {})
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    return public(await o.get_order(order_id))


# --- client endpoints -----------------------------------------------------------------------

@app.post("/orders", status_code=201)
async def create_order(body: OrderIn, request: Request) -> dict[str, Any]:
    # The public site form posts here; cap intake so a flood cannot burn the LLM budget.
    recent = await orch(request).pool.fetchval("SELECT count(*) FROM orders WHERE created_at > now() - interval '1 hour'")
    if recent >= get_settings().intake_hourly_limit:
        raise HTTPException(429, "too many orders right now, try again later")
    video = None
    if body.video is not None:
        if not await uploads.claim(orch(request).pool, body.video.id, body.video.token):
            raise HTTPException(422, "the video upload is not finished or not found")
        video = body.video.id
    order, token = await orch(request).create_order(**body.model_dump(exclude={"video"}), video_upload=video)
    return {"order": public(order), "client_token": token,
            "portal_url": portal_url(get_settings().public_base_url, order["id"], token)}


@app.get("/orders/{order_id}")
async def get_order(request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    return {
        "order": public(order),
        "spec": await o.latest_artifact(order["id"], "spec"),
        "estimate": await o.latest_artifact(order["id"], "estimate"),
        "messages": await o.messages(order["id"]),
    }


@app.post("/orders/{order_id}/messages")
async def post_message(body: MessageIn, request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    await o.add_message(order["id"], "in", body.text)
    if order["state"] == State.AWAITING_CLIENT_INFO:
        return await _fire(o, order["id"], Ev.CLIENT_REPLIED, "client")
    await o.log(order["id"], "client_message", "client", {}, kind="client")
    return public(order)


@app.post("/orders/{order_id}/estimate")
async def decide_estimate(body: DecisionIn, request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    if body.comment:
        await o.add_message(order["id"], "in", body.comment)
    event = Ev.ESTIMATE_APPROVED if body.approved else Ev.ESTIMATE_REJECTED
    return await _fire(o, order["id"], event, "client", {"comment": body.comment})


@app.post("/orders/{order_id}/acceptance")
async def decide_acceptance(body: DecisionIn, request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    if body.comment:
        await o.add_message(order["id"], "in", body.comment)
    event = Ev.ACCEPTED if body.approved else Ev.CHANGES_REQUESTED
    return await _fire(o, order["id"], event, "client", {"comment": body.comment})


@app.post("/orders/{order_id}/cancel")
async def cancel(request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    return await _fire(orch(request), order["id"], Ev.CANCEL, "client")


SSE_WINDOW_SEC = 240  # below the function's maxDuration; EventSource reconnects with Last-Event-ID


@app.get("/orders/{order_id}/events")
async def stream_events(
    request: Request, order: dict = Depends(client_order), last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """Server-sent events: the live status feed for the client portal.

    Each connection lives up to SSE_WINDOW_SEC; the browser's EventSource reconnects on its own and
    sends Last-Event-ID, so the feed resumes exactly where it stopped."""
    o = orch(request)
    start_after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def gen():
        last = start_after
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SSE_WINDOW_SEC
        yield "retry: 2000\n\n"
        while loop.time() < deadline and not await request.is_disconnected():
            for ev in await o.events(order["id"], last):
                last = ev["id"]
                payload = {k: ev[k] for k in ("id", "name", "from_state", "to_state", "actor")}
                payload["at"] = ev["created_at"].isoformat()
                yield f"id: {ev['id']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- operator endpoints ---------------------------------------------------------------------

@app.get("/admin/orders", dependencies=[Depends(operator)])
async def list_orders(request: Request, state: State | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM orders" + (" WHERE state = $1" if state else "") + " ORDER BY id DESC LIMIT 200"
    rows = await orch(request).pool.fetch(q, *([state] if state else []))
    return [public(dict(r)) | {"tokens_used": r["tokens_used"], "token_budget": r["token_budget"]} for r in rows]


@app.get("/admin/orders/{order_id}/journal", dependencies=[Depends(operator)])
async def journal(order_id: int, request: Request) -> list[dict[str, Any]]:
    return await orch(request).events(order_id)


@app.post("/admin/orders/{order_id}/resume", dependencies=[Depends(operator)])
async def resume(order_id: int, body: OperatorNote, request: Request) -> dict[str, Any]:
    return await _fire(orch(request), order_id, Ev.RESUME, "operator", {"note": body.note})


@app.post("/admin/orders/{order_id}/redeploy", dependencies=[Depends(operator)])
async def redeploy_order(order_id: int, body: OperatorNote, request: Request) -> dict[str, Any]:
    """Deploy a delivered order again from its latest code and the platform's current files."""
    o = orch(request)
    try:
        order = await o.get_order(order_id)
    except KeyError:
        raise HTTPException(404, "order not found") from None
    if order["state"] != State.DELIVERED:
        raise HTTPException(409, "only a delivered order can be redeployed")
    try:
        async with o.pool.acquire() as conn:
            task_id = await queue.enqueue(conn, order_id, "redeploy_production", {"note": body.note})
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "another task of this order is still running") from None
    await o.log(order_id, "redeploy_requested", "operator", {"note": body.note[:500]}, kind="operator")
    return {"task": task_id}


@app.post("/admin/orders/{order_id}/budget", dependencies=[Depends(operator)])
async def raise_budget(order_id: int, request: Request, add_tokens: int = 500_000) -> dict[str, Any]:
    o = orch(request)
    await o.pool.execute("UPDATE orders SET token_budget = token_budget + $2 WHERE id = $1", order_id, add_tokens)
    await o.log(order_id, "budget_raised", "operator", {"add_tokens": add_tokens}, kind="system")
    return public(await o.get_order(order_id))


@app.post("/admin/orders/{order_id}/payment", dependencies=[Depends(operator)])
async def payment_received(order_id: int, request: Request) -> dict[str, Any]:
    """The operator confirms a payment received outside Stripe (bank transfer, cash)."""
    o = orch(request)
    try:
        order = await o.get_order(order_id)
    except KeyError:
        raise HTTPException(404, "order not found") from None
    stage = {State.AWAITING_PAYMENT: "prepayment", State.AWAITING_FINAL_PAYMENT: "final"}.get(State(order["state"]))
    if stage is None:
        raise HTTPException(409, f"order is {order['state']}; no payment is due")
    try:
        recorded = await o.record_payment(stage=stage, order_id=order_id, actor="operator")
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    if not recorded:
        raise HTTPException(409, "no pending payment for this order yet; the worker creates it within a minute")
    return public(await o.get_order(order_id))


@app.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Stripe calls this when a Checkout Session completes or expires. The signature is checked
    against STRIPE_WEBHOOK_SECRET before anything is trusted."""
    payload = await request.body()
    secret = await webhook_secret(get_settings(), orch(request).pool)
    try:
        event = verify_stripe_webhook(payload, request.headers.get("stripe-signature"), secret)
    except WebhookError as e:
        raise HTTPException(400, str(e)) from None

    o = orch(request)
    obj = (event.get("data") or {}).get("object") or {}
    kind = event.get("type")
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and obj.get("payment_status") == "paid":
        try:
            await o.record_payment(stage=(obj.get("metadata") or {}).get("stage", ""), session_id=obj.get("id"), actor="stripe")
        except TransitionError:
            pass  # e.g. the order was cancelled meanwhile; the payment row still says paid for the operator
    elif kind == "checkout.session.expired":
        row = await o.pool.fetchrow(
            "UPDATE payments SET status = 'expired' WHERE provider_session_id = $1 AND status = 'pending' "
            "RETURNING order_id, stage", obj.get("id"))
        if row:
            # Still waiting for this payment? Send a fresh link.
            order = await o.get_order(row["order_id"])
            waiting = {"prepayment": State.AWAITING_PAYMENT, "final": State.AWAITING_FINAL_PAYMENT}[row["stage"]]
            if order["state"] == waiting:
                async with o.pool.acquire() as conn:
                    try:
                        await queue.enqueue(conn, row["order_id"], TASK_FOR_STATE[waiting])
                    except asyncpg.UniqueViolationError:
                        pass  # a request task is already queued
    return {"received": "ok"}


PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TPF Technology</title><style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#07090C;color:#E6EDF2;
font-family:system-ui,sans-serif;text-align:center;padding:24px}}h1{{font-size:28px;margin:0 0 12px}}p{{color:#B7C3CC;margin:0}}b{{color:#5CE1F0}}</style>
</head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


@app.get("/pay/done", include_in_schema=False)
async def pay_done(order: int = 0) -> HTMLResponse:
    return HTMLResponse(PAGE.format(lang="ru", title="Спасибо! Оплата получена",
                                    text=f"Заказ <b>№{order}</b>: работа продолжится автоматически. Мы сообщим о следующем шаге."))


@app.get("/pay/cancelled", include_in_schema=False)
async def pay_cancelled(order: int = 0) -> HTMLResponse:
    return HTMLResponse(PAGE.format(lang="ru", title="Оплата не завершена",
                                    text=f"Заказ <b>№{order}</b>: ссылка на оплату остаётся в силе, вы можете вернуться к ней позже."))


@app.get("/health")
async def health(request: Request):
    """Liveness plus a database round trip. On failure, names the error class so a deploy can be
    diagnosed without log access; the message is shown with any credentials stripped."""
    from fastapi.responses import JSONResponse

    try:
        await orch(request).pool.fetchval("SELECT count(*) FROM orders")
    except Exception as e:  # noqa: BLE001
        dsn = get_settings().database_url
        msg = str(e)
        if "@" in dsn:
            msg = msg.replace(dsn.split("@")[0], "***")
        return JSONResponse({"status": "error", "database": f"{type(e).__name__}: {msg[:300]}"}, status_code=503)
    s = get_settings()
    payments = {"provider": s.payment_provider}
    if s.payment_provider == "stripe":
        payments["webhook_secret"] = bool(await webhook_secret(s, orch(request).pool))
    return {"status": "ok", "database": "ok", "payments": payments}


def run() -> None:
    import uvicorn

    uvicorn.run("tpf.api.main:app", host="0.0.0.0", port=8000)


SITE_DIR = Path(__file__).resolve().parent.parent / "site"


def _page(name: str) -> FileResponse:
    """A page of the studio site with its text already in the HTML (scripts/prerender_site.py); the source
    page, which fills itself in the browser, when the copy is missing."""
    path = SITE_DIR / f"index.{name}.html"
    return FileResponse(path if path.is_file() else SITE_DIR / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/", include_in_schema=False)
async def site() -> FileResponse:
    """The public TPF Technology site; its contact form posts to /orders."""
    return _page("root")


SITE_LANGS = {"ru": "ru", "en": "en", "zh": "zh-Hans"}
SITE_URL = "https://tpf-technology.com"


def _lang_page(lang: str):
    async def serve() -> FileResponse:
        return _page(lang)
    return serve


for _lang in SITE_LANGS:
    app.get(f"/{_lang}", include_in_schema=False)(_lang_page(_lang))


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> Response:
    # Order pages, the admin area and the endpoints forms and applications post to are nobody's search result.
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: /{p}" for p in ("portal/", "admin", "orders", "forms/", "apps/", "uploads", "webhooks/", "pay/")),
             "", f"Sitemap: {SITE_URL}/sitemap.xml", ""]
    return Response("\n".join(lines), media_type="text/plain; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    changed = datetime.fromtimestamp((SITE_DIR / "index.html").stat().st_mtime, tz=timezone.utc).date().isoformat()
    alternates = "".join(f'<xhtml:link rel="alternate" hreflang="{code}" href="{SITE_URL}/{lang}"/>' for lang, code in SITE_LANGS.items()) \
        + f'<xhtml:link rel="alternate" hreflang="x-default" href="{SITE_URL}/"/>'
    urls = "".join(f"<url><loc>{SITE_URL}/{path}</loc><lastmod>{changed}</lastmod>{alternates}</url>" for path in ("", *SITE_LANGS))
    urls += f"<url><loc>{SITE_URL}/privacy</loc></url>"
    xml = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           f'xmlns:xhtml="http://www.w3.org/1999/xhtml">{urls}</urlset>')
    return Response(xml, media_type="application/xml; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/privacy", include_in_schema=False)
async def privacy() -> FileResponse:
    return FileResponse(SITE_DIR / "privacy.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/case-zerno.jpg", include_in_schema=False)
async def case_zerno() -> FileResponse:
    return FileResponse(SITE_DIR / "case-zerno.jpg", media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


SITE_ASSETS = {"apple-touch-icon.png": "image/png", "apple-touch-icon-precomposed.png": "image/png", "icon-192.png": "image/png",
               "icon-512.png": "image/png", "favicon.png": "image/png", "favicon.ico": "image/png",
               "manifest.webmanifest": "application/manifest+json", "case-gge.jpg": "image/jpeg", "case-brigada.jpg": "image/jpeg", "case-mastera.jpg": "image/jpeg", "case-glina-shop.jpg": "image/jpeg", "case-glina-app.jpg": "image/jpeg", "case-glina-bot.jpg": "image/jpeg", "case-yoga.jpg": "image/jpeg",
               "demo-mug-1.jpg": "image/jpeg", "demo-mug-2.jpg": "image/jpeg", "demo-plate-1.jpg": "image/jpeg", "demo-plate-2.jpg": "image/jpeg", "demo-vase-1.jpg": "image/jpeg", "demo-vase-2.jpg": "image/jpeg"}
SITE_ALIASES = {"apple-touch-icon-precomposed.png": "apple-touch-icon.png", "favicon.ico": "favicon.png"}


def _asset(name: str):
    async def serve() -> FileResponse:
        return FileResponse(SITE_DIR / SITE_ALIASES.get(name, name), media_type=SITE_ASSETS[name],
                            headers={"Cache-Control": "public, max-age=86400"})
    return serve


for _name in SITE_ASSETS:      # the icon a phone puts on its home screen, and what describes the site as an app
    app.get(f"/{_name}", include_in_schema=False)(_asset(_name))


@app.get("/brand-mark.jpg", include_in_schema=False)
async def brand_mark() -> FileResponse:
    return FileResponse(SITE_DIR / "brand-mark.jpg", media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/brand-hero.jpg", include_in_schema=False)
async def brand_hero() -> FileResponse:
    return FileResponse(SITE_DIR / "brand-hero.jpg", media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/brand.jpg", include_in_schema=False)
async def brand() -> FileResponse:
    return FileResponse(SITE_DIR / "brand.jpg", media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


from tpf.api.portal import router as portal_router  # noqa: E402  (after app: the portal reads app.state)

app.include_router(portal_router)

from tpf.api.admin import router as admin_router  # noqa: E402

app.include_router(admin_router)

from tpf.api.forms import router as forms_router  # noqa: E402

app.include_router(forms_router)
app.include_router(uploads.router)
app.include_router(apps.router)
