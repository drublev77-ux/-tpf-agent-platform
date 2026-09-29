"""Client and operator notifications. Pilot: log or a webhook (n8n, Telegram bot, e-mail relay)."""
from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from tpf.config import Settings

log = logging.getLogger("tpf.notify")

# Short client-facing texts per waiting state, in the client's language.
TEMPLATES: dict[str, dict[str, str]] = {
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


def make_notifier(settings: Settings) -> Notifier:
    if settings.notifier == "webhook" and settings.notify_webhook_url:
        return WebhookNotifier(settings.notify_webhook_url)
    return LogNotifier()
