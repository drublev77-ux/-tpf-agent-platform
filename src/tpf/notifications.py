"""Client and operator notifications: log, a webhook (n8n, Telegram), or e-mail via Resend / Brevo.

E-mail goes over HTTPS APIs because hosting providers (Railway) block outbound SMTP.
Every client e-mail carries a button to the order's private portal page.
"""
from __future__ import annotations

import logging
import re
from email.utils import formataddr, parseaddr
from html import escape
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from tpf.config import Settings

log = logging.getLogger("tpf.notify")

# Short client-facing texts per waiting state, in the client's language.
TEMPLATES: dict[str, dict[str, str]] = {
    "received": {
        "ru": "Спасибо! Мы получили вашу заявку, заказ №{id}. Команда уже изучает задачу — следующим письмом пришлём вопросы или ТЗ со сметой.",
        "en": "Thank you! We've received your request, order #{id}. The team is on it — next we'll send questions or the spec with an estimate.",
        "zh": "感谢！我们已收到您的需求，订单 #{id}。团队正在研究——接下来我们会发送问题或需求规格书与报价。",
    },
    "awaiting_client_info": {
        "ru": "Уточните, пожалуйста, несколько деталей по заказу №{id}:\n{details}",
        "en": "We need a few details for order #{id}:\n{details}",
        "zh": "关于订单 #{id}，我们需要确认以下细节：\n{details}",
    },
    "awaiting_estimate": {
        "ru": "Готовы ТЗ и смета по заказу №{id}: {details}. Подтвердите смету, чтобы мы начали работу.",
        "en": "The spec and estimate for order #{id} are ready: {details}. Approve the estimate to start work.",
        "zh": "订单 #{id} 的需求规格书与报价已完成：{details}。请确认报价以便开工。",
    },
    "awaiting_payment": {
        "ru": "Смета по заказу №{id} подтверждена. Для старта внесите предоплату: {details}.",
        "en": "Estimate for order #{id} approved. Please make the prepayment to start: {details}.",
        "zh": "订单 #{id} 的报价已确认。请支付预付款以开始：{details}。",
    },
    "awaiting_acceptance": {
        "ru": "Заказ №{id} готов к приёмке. Посмотрите результат: {details}. Примите работу или напишите замечания.",
        "en": "Order #{id} is ready for review: {details}. Accept it or send us your comments.",
        "zh": "订单 #{id} 已可验收：{details}。请验收或提出修改意见。",
    },
    "delivered": {
        "ru": "Заказ №{id} сдан. Рабочая версия: {details}. Спасибо, что выбрали TPF Technology!",
        "en": "Order #{id} is delivered. Live version: {details}. Thank you for choosing TPF Technology!",
        "zh": "订单 #{id} 已交付。正式版本：{details}。感谢选择 TPF Technology！",
    },
}


# Sent when an order has waited for the client for days (orchestrator/reminders.py). The e-mail carries the
# button to the order page, where every one of these decisions is made.
REMINDERS: dict[str, dict[str, str]] = {
    "awaiting_client_info": {
        "ru": "Напоминаем о заказе №{id}: мы ждём ваших ответов на вопросы, чтобы подготовить ТЗ и смету. Ответить можно на странице заказа.",
        "en": "A reminder about order #{id}: we are waiting for your answers to prepare the spec and the estimate. You can reply on your order page.",
        "zh": "关于订单 #{id} 的提醒：我们正在等待您的答复，以便准备需求规格书与报价。您可以在订单页面回复。",
    },
    "awaiting_estimate": {
        "ru": "Напоминаем: ТЗ и смета по заказу №{id} готовы и ждут вашего решения. Утвердите смету или напишите, что изменить, — на странице заказа.",
        "en": "A reminder: the spec and estimate for order #{id} are ready and waiting for your decision. Approve the estimate or tell us what to change on your order page.",
        "zh": "提醒：订单 #{id} 的需求规格书与报价已完成，等待您的决定。请在订单页面确认报价，或告诉我们需要修改之处。",
    },
    "awaiting_payment": {
        "ru": "Напоминаем: заказ №{id} ждёт предоплату — после неё мы сразу начнём работу. Все детали — на странице заказа.",
        "en": "A reminder: order #{id} is waiting for the prepayment — we start work as soon as it arrives. The details are on your order page.",
        "zh": "提醒：订单 #{id} 正在等待预付款——收到后我们会立即开工。详情请见订单页面。",
    },
    "awaiting_acceptance": {
        "ru": "Напоминаем: заказ №{id} готов и ждёт вашей проверки. Посмотрите результат и примите работу или напишите замечания — на странице заказа.",
        "en": "A reminder: order #{id} is ready and waiting for your review. Look at the result and accept the work or send your comments on your order page.",
        "zh": "提醒：订单 #{id} 已完成，等待您验收。请在订单页面查看成果，然后验收或提出修改意见。",
    },
    "awaiting_final_payment": {
        "ru": "Напоминаем: заказ №{id} принят и ждёт оплаты остатка — после неё мы опубликуем рабочую версию. Все детали — на странице заказа.",
        "en": "A reminder: order #{id} is accepted and waiting for the final payment — we publish the live version as soon as it arrives. The details are on your order page.",
        "zh": "提醒：订单 #{id} 已验收，正在等待尾款——收到后我们会发布正式版本。详情请见订单页面。",
    },
}
REMINDER_LAST = {
    "ru": "Это последнее напоминание: заказ останется на вашей странице, и вы сможете вернуться к нему, когда будет удобно.",
    "en": "This is the last reminder: the order stays on your page, and you can come back to it whenever it suits you.",
    "zh": "这是最后一次提醒：订单会保留在您的页面上，您可以在方便时随时回来处理。",
}


SUBJECT = {"ru": "TPF Technology — заказ №{id}", "en": "TPF Technology — order #{id}", "zh": "TPF Technology — 订单 #{id}"}
BUTTON = {"ru": "Открыть заказ", "en": "Open your order", "zh": "查看订单"}
FOOTER = {
    "ru": "Все детали, оплата и приёмка — на странице заказа. Ссылка личная, не пересылайте её.",
    "en": "Details, payments and acceptance are on your order page. The link is private — please don't forward it.",
    "zh": "所有细节、付款与验收均在订单页面。此链接为私人链接，请勿转发。",
}


def portal_url(base: str, order_id: int, token: str) -> str:
    return f"{base.rstrip('/')}/portal/{order_id}?t={quote(token, safe='')}"


def render(state: str, language: str, order_id: int, details: str) -> str | None:
    variants = TEMPLATES.get(state)
    if not variants:
        return None
    return variants.get(language, variants["en"]).format(id=order_id, details=details)


class Notifier(Protocol):
    async def to_client(self, order: dict[str, Any], text: str) -> None: ...
    async def to_operator(self, order: dict[str, Any], text: str) -> None: ...


class LogNotifier:
    async def to_client(self, order: dict[str, Any], text: str) -> None:
        log.info("→ client %s (order %s): %s", order["client_email"], order["id"], text)

    async def to_operator(self, order: dict[str, Any], text: str) -> None:
        log.warning("→ operator (order %s): %s", order["id"], text)


class WebhookNotifier:
    """POSTs {audience, order_id, email, channel, text} to one URL; route it on to e-mail or messengers."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def _post(self, audience: str, order: dict[str, Any], text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(self.url, json={
                "audience": audience, "order_id": order["id"], "email": order["client_email"],
                "channel": order["channel"], "language": order["language"], "text": text,
            })
            r.raise_for_status()

    async def to_client(self, order: dict[str, Any], text: str) -> None:
        await self._post("client", order, text)

    async def to_operator(self, order: dict[str, Any], text: str) -> None:
        await self._post("operator", order, text)


_URL = re.compile(r"https://[^\s<>\"']+")


def _linkify(text: str) -> str:
    html = _URL.sub(lambda m: f'<a href="{m[0]}" style="color:#7c5cff">{m[0]}</a>', escape(text))
    return html.replace("\n", "<br>")


def email_html(text: str, language: str, link: str | None) -> str:
    lang = language if language in BUTTON else "en"
    button = (
        f'<p style="margin:28px 0"><a href="{escape(link)}" style="background:#7c5cff;color:#fff;'
        f'padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600">{BUTTON[lang]}</a></p>'
        f'<p style="color:#8a8f98;font-size:12px">{FOOTER[lang]}</p>'
    ) if link else ""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:0 auto;'
        'padding:24px;color:#1b1d22;line-height:1.55">'
        '<div style="font-weight:700;font-size:18px;letter-spacing:.3px;margin-bottom:20px">TPF Technology</div>'
        f'<div style="font-size:15px">{_linkify(text)}</div>{button}</div>'
    )


class EmailNotifier:
    """Sends e-mail through Resend or Brevo. Client mails link to the order portal."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self._client = client

    def _providers(self) -> list[tuple[str, str, str]]:
        """(provider, API key, sender) in the order they are tried: the main one, then the fallback if it is set."""
        s = self.s
        out = [(s.email_provider, s.email_api_key, s.email_from)]
        if s.email_fallback_provider in ("resend", "brevo") and s.email_fallback_api_key:
            out.append((s.email_fallback_provider, s.email_fallback_api_key, s.email_fallback_from or s.email_from))
        return out

    async def send(self, to: str, subject: str, text: str, html: str, *, sender_name: str = "", reply_to: str = "") -> None:
        """`sender_name` and `reply_to` are for mail sent on behalf of a client's application: the address stays
        ours (the only one the provider has verified), the name and the reply address are theirs.
        A letter the main provider refuses (a lapsed key, an IP allowlist, an outage) goes through the fallback
        provider when one is set; the error is raised only when every provider has refused, and names them all."""
        errors: list[str] = []
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            for provider, key, sender in self._providers():
                try:
                    await self._post(client, provider, key, sender, to, subject, text, html,
                                     sender_name=sender_name, reply_to=reply_to or self.s.email_reply_to)
                except (RuntimeError, httpx.HTTPError) as e:
                    errors.append(str(e) or f"{provider} {type(e).__name__}")
                    continue
                if errors:
                    log.warning("mail to %s went through %s after: %s", to, provider, " | ".join(errors))
                return
        finally:
            if self._client is None:
                await client.aclose()
        raise RuntimeError(" | ".join(errors))

    @staticmethod
    async def _post(client: httpx.AsyncClient, provider: str, key: str, sender: str, to: str, subject: str,
                    text: str, html: str, *, sender_name: str, reply_to: str) -> None:
        name, addr = parseaddr(sender)
        name = sender_name or name
        if provider == "resend":
            url, headers = "https://api.resend.com/emails", {"Authorization": f"Bearer {key}"}
            body: dict[str, Any] = {"from": formataddr((name, addr)) if sender_name else sender, "to": [to], "subject": subject, "text": text, "html": html}
            if reply_to:
                body["reply_to"] = reply_to
        else:  # brevo
            url, headers = "https://api.brevo.com/v3/smtp/email", {"api-key": key}
            body = {"sender": {"email": addr, "name": name or "TPF Technology"}, "to": [{"email": to}],
                    "subject": subject, "textContent": text, "htmlContent": html}
            if reply_to:
                body["replyTo"] = {"email": reply_to}
        r = await client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"{provider} {r.status_code}: {r.text[:300]}")

    async def to_client(self, order: dict[str, Any], text: str) -> None:
        lang = order.get("language") or "en"
        link = portal_url(self.s.public_base_url, order["id"], order["client_token"]) if order.get("client_token") else None
        plain = f"{text}\n\n{BUTTON.get(lang, BUTTON['en'])}: {link}" if link else text
        await self.send(order["client_email"], SUBJECT.get(lang, SUBJECT["en"]).format(id=order["id"]),
                        plain, email_html(text, lang, link))

    async def to_operator(self, order: dict[str, Any], text: str) -> None:
        log.warning("→ operator (order %s): %s", order["id"], text)
        if self.s.operator_email:
            admin = portal_url(self.s.public_base_url, order["id"], order.get("client_token") or "")
            await self.send(self.s.operator_email, f"[TPF] Заказ #{order['id']}", f"{text}\n\n{admin}",
                            email_html(f"{text}\n\n{admin}", "ru", None))


class ChainNotifier:
    """Fans every notification out to all configured channels (e-mail AND webhook), so the
    lead pipeline keeps receiving events while Brevo letters to clients stay on."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = notifiers

    async def to_client(self, order: dict[str, Any], text: str) -> None:
        for n in self._notifiers:
            await n.to_client(order, text)

    async def to_operator(self, order: dict[str, Any], text: str) -> None:
        for n in self._notifiers:
            await n.to_operator(order, text)


def make_notifier(settings: Settings) -> Notifier:
    parts: list[Notifier] = []
    if settings.email_enabled:
        parts.append(EmailNotifier(settings))
    if settings.notifier == "webhook" and settings.notify_webhook_url:
        parts.append(WebhookNotifier(settings.notify_webhook_url))
    if len(parts) == 1:
        return parts[0]              # one channel: no wrapper, so isinstance checks stay honest
    if parts:
        return ChainNotifier(*parts)
    return LogNotifier()
