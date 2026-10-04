"""A shop owner's own Stripe account: the platform checks the key the owner entered and registers the
webhook that tells the shop about payments. Charging is done by the shop itself (tpf/payments.ts)."""
from __future__ import annotations

import re
from urllib.parse import urlencode

import httpx

KEY_RE = re.compile(r"^(sk|rk)_(test|live)_[A-Za-z0-9]{16,}$")
EVENTS = ["checkout.session.completed", "checkout.session.async_payment_succeeded"]


class StripeKeyError(RuntimeError):
    pass


def key_mode(key: str) -> str:
    return "live" if "_live_" in key else "test"


class ShopStripe:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(base_url="https://api.stripe.com", timeout=20)

    async def _call(self, key: str, method: str, path: str, data: list[tuple[str, str]] | None = None) -> tuple[int, dict]:
        try:
            content = urlencode(data).encode() if data else None
            headers = {"Authorization": f"Bearer {key}"} | ({"Content-Type": "application/x-www-form-urlencoded"} if data else {})
            r = await self.client.request(method, path, content=content, headers=headers)
            return r.status_code, r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise StripeKeyError(f"Stripe {path}: {type(e).__name__}") from None

    async def check(self, key: str) -> None:
        """Raise StripeKeyError unless the key works and may manage webhook endpoints."""
        status, body = await self._call(key, "GET", "/v1/webhook_endpoints?limit=1")
        if status != 200:
            raise StripeKeyError(str((body.get("error") or {}).get("message", status))[:300])

    async def ensure_webhook(self, key: str, url: str, endpoint_id: str = "") -> tuple[str, str | None]:
        """Point the shop's webhook endpoint at `url`. Returns (endpoint id, signing secret); the secret is
        None when an existing endpoint was moved, because Stripe reveals it only at creation."""
        if endpoint_id:
            status, body = await self._call(key, "POST", f"/v1/webhook_endpoints/{endpoint_id}", [("url", url), ("disabled", "false")])
            if status == 200:
                return endpoint_id, None
        data = [("url", url), ("description", "TPF shop payments")] + [("enabled_events[]", e) for e in EVENTS]
        status, body = await self._call(key, "POST", "/v1/webhook_endpoints", data)
        if status != 200 or not body.get("secret"):
            raise StripeKeyError(str((body.get("error") or {}).get("message", status))[:300])
        return body["id"], body["secret"]
