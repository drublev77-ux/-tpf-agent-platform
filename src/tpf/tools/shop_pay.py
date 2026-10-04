"""Payment providers a shop owner can connect besides Stripe: YooKassa (Russia) and bePaid (Belarus).

The platform only checks the credentials the owner entered; payments are made by the shop itself
(tpf/payments.ts), with the owner's credentials, into the owner's account."""
from __future__ import annotations

import base64
import re

import httpx

PROVIDERS = ("stripe", "yookassa", "bepaid")
NAMES = {"stripe": "Stripe", "yookassa": "ЮKassa", "bepaid": "bePaid"}
SHOP_ID_RE = re.compile(r"^\d{1,12}$")
YOOKASSA_KEY_RE = re.compile(r"^(test|live)_[A-Za-z0-9_-]{16,200}$")
BEPAID_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,200}$")
NOTIFY_PATH = "/api/tpf-pay"      # where the shop receives YooKassa and bePaid notifications


class ShopPayError(RuntimeError):
    pass


def valid(provider: str, shop_id: str, key: str) -> bool:
    """Do the credentials at least look like this provider's?"""
    if provider == "yookassa":
        return bool(SHOP_ID_RE.match(shop_id) and YOOKASSA_KEY_RE.match(key))
    if provider == "bepaid":
        return bool(SHOP_ID_RE.match(shop_id) and BEPAID_KEY_RE.match(key))
    return False


def mode_of(provider: str, key: str) -> str:
    """'test' or 'live'. A YooKassa key says which it is. bePaid has one key for both: the shop marks its
    payments as test ones on the review build and as real ones once released, so the key counts as live."""
    return "test" if provider == "yookassa" and key.startswith("test_") else "live"


def effective_mode(provider: str, mode: str, env: str) -> str:
    """The mode payments actually run in at a deployment."""
    return ("test" if env == "stg" else "live") if provider == "bepaid" else mode


class ShopPay:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=20)

    async def check(self, provider: str, shop_id: str, key: str) -> None:
        """Raise ShopPayError unless the provider accepts these credentials."""
        auth = {"Authorization": "Basic " + base64.b64encode(f"{shop_id}:{key}".encode()).decode()}
        try:
            if provider == "yookassa":
                r = await self.client.get("https://api.yookassa.ru/v3/me", headers=auth)
                ok = r.status_code == 200
            elif provider == "bepaid":
                # bePaid has no "who am I" call: ask for a test payment page, which costs nothing and is never paid.
                r = await self.client.post(
                    "https://checkout.bepaid.by/ctp/api/checkouts",
                    headers=auth | {"Accept": "application/json", "X-API-Version": "2"},
                    json={"checkout": {"test": True, "transaction_type": "payment", "settings": {},
                                       "order": {"currency": "BYN", "amount": 100, "description": "TPF: проверка подключения"}}})
                ok = r.status_code not in (401, 403) and r.status_code < 500   # 422 = the keys are fine, the order is not
            else:
                raise ShopPayError(f"unknown provider {provider!r}")
        except httpx.HTTPError as e:
            raise ShopPayError(f"{NAMES[provider]}: {type(e).__name__}") from None   # never echo the credentials
        if not ok:
            raise ShopPayError(f"{NAMES[provider]} refused the credentials ({r.status_code})")
