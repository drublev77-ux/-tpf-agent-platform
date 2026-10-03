"""The two Telegram Bot API calls the platform itself makes: check a client's bot token and point the
bot's webhook at the deployed application. Everything else is done by the application."""
from __future__ import annotations

import hashlib
import re

import httpx

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,60}$")


class TelegramError(RuntimeError):
    pass


def webhook_secret(session_secret: str) -> str:
    """Must match webhookSecret() in the application template (tpf/telegram.ts)."""
    return hashlib.sha256(f"tg:{session_secret}".encode()).hexdigest()


class TelegramAPI:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=20)

    async def _call(self, token: str, method: str, params: dict | None = None) -> dict:
        try:
            r = await self.client.post(f"https://api.telegram.org/bot{token}/{method}", json=params or {})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"Telegram {method}: {type(e).__name__}") from None   # never echo the URL: it holds the token
        if not body.get("ok"):
            raise TelegramError(f"Telegram {method}: {body.get('description', r.status_code)}")
        return body.get("result") if isinstance(body.get("result"), dict) else {}

    async def get_me(self, token: str) -> str:
        """The bot's username; raises TelegramError when the token is not a working bot token."""
        return str((await self._call(token, "getMe")).get("username", ""))

    async def set_webhook(self, token: str, url: str, secret: str) -> None:
        await self._call(token, "setWebhook", {
            "url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True, "max_connections": 20,
        })
