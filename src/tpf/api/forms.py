"""Contact forms of the sites the platform builds. A site is a static export, so its form posts
here: the message is stored and e-mailed to the site's owner (the order's client)."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tpf.config import get_settings
from tpf.notifications import EmailNotifier, email_html
from tpf.orchestrator.states import State
from tpf.tools import hooks

log = logging.getLogger("tpf.forms")
router = APIRouter(include_in_schema=False)

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type", "Access-Control-Max-Age": "86400"}
MAX_BODY = 20_000
PER_HOUR = 30                      # per site: enough for a small business, useless for a flood
LIVE = {State.CHECKS, State.STAGING, State.AWAITING_ACCEPTANCE, State.AWAITING_FINAL_PAYMENT, State.RELEASE, State.DELIVERED}
FIELDS = {"name": 200, "contact": 200, "message": 5000, "page": 300}

MAIL = {
    "ru": ("Новое сообщение с вашего сайта", "Имя", "Контакт", "Сообщение", "Страница",
           "Это письмо отправлено формой на вашем сайте. Чтобы ответить посетителю, напишите ему на указанный контакт."),
    "en": ("New message from your website", "Name", "Contact", "Message", "Page",
           "This e-mail was sent by the form on your website. To answer the visitor, write to the contact above."),
    "zh": ("您的网站收到新留言", "姓名", "联系方式", "留言", "页面",
           "此邮件由您网站上的表单发送。如需回复访客，请通过上述联系方式与其联系。"),
}


def _reply(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=CORS)


@router.options("/forms/{order_id}")
async def form_preflight(order_id: int) -> Response:
    return Response(status_code=204, headers=CORS)


@router.post("/forms/{order_id}")
async def form_submit(order_id: int, request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _reply(413, {"ok": False})
    try:
        body = json.loads(raw)
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        return _reply(400, {"ok": False})
    if body.get("website"):                       # the hidden field only bots fill: accept and drop
        return _reply(200, {"ok": True})
    data = {k: str(body.get(k, "")).strip()[:limit] for k, limit in FIELDS.items()}
    if not (data["name"] and data["contact"] and data["message"]):
        return _reply(422, {"ok": False, "error": "name, contact and message are required"})

    o = request.app.state.orch
    try:
        order = await o.get_order(order_id)
    except KeyError:
        return _reply(404, {"ok": False})
    if order["state"] not in LIVE:
        return _reply(404, {"ok": False})
    recent = await o.pool.fetchval(
        "SELECT count(*) FROM form_submissions WHERE order_id = $1 AND created_at > now() - interval '1 hour'", order_id)
    if recent >= PER_HOUR:
        return _reply(429, {"ok": False})
    sub_id = await o.pool.fetchval(
        "INSERT INTO form_submissions (order_id, data) VALUES ($1, $2) RETURNING id", order_id, data)

    s = get_settings()
    if s.email_enabled:
        subject, l_name, l_contact, l_message, l_page, footer = MAIL.get(order["language"], MAIL["en"])
        text = (f"{l_name}: {data['name']}\n{l_contact}: {data['contact']}\n"
                + (f"{l_page}: {data['page']}\n" if data["page"] else "")
                + f"\n{l_message}:\n{data['message']}\n\n— \n{footer}")
        try:
            await EmailNotifier(s).send(order["client_email"], subject, text, email_html(text, order["language"], None))
            await o.pool.execute("UPDATE form_submissions SET delivered = true WHERE id = $1", sub_id)
        except Exception:  # noqa: BLE001 — the message is stored; the visitor still gets a success
            log.exception("form message %s for order %s was stored but not e-mailed", sub_id, order_id)
    await hooks.emit(o.pool, order_id, "record.created", {
        "subject": data["name"], "contact": data["contact"], "text": data["message"], "page": data["page"]})
    return _reply(200, {"ok": True})
