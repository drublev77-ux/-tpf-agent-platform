"""Events of a delivered product, sent to the address its owner chose (an n8n, Zapier or Make webhook, or a CRM).

The address comes from a client, so it is treated as hostile: https only, no credentials in it, and every name
it resolves to must be a public address — checked again before each delivery, which follows no redirects."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_URL = 500
TIMEOUT = 5.0


class HookError(ValueError):
    pass


def parse(url: str) -> tuple[str, int]:
    """The host and port of an acceptable address; HookError otherwise."""
    if len(url) > MAX_URL or any(c.isspace() for c in url):
        raise HookError("address")
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as e:
        raise HookError("address") from e
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or "." not in host:
        raise HookError("address")
    if host.endswith((".local", ".internal", ".localhost")):
        raise HookError("address")
    return host, port


async def check_public(url: str) -> None:
    """HookError unless every address the host resolves to is a public one."""
    host, port = parse(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise HookError("host") from e
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise HookError("host")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def deliver(url: str, secret: str, event: dict[str, Any]) -> int:
    """POST the event. Returns the HTTP status, or 0 when the address could not be reached or is not allowed."""
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    try:
        await check_public(url)
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            r = await client.post(url, content=body, headers={
                "content-type": "application/json", "user-agent": "TPF-Technology-Hooks/1",
                "x-tpf-event": str(event.get("event", "")), "x-tpf-timestamp": timestamp,
                "x-tpf-signature": sign(secret, timestamp, body)})
        return r.status_code
    except (HookError, httpx.HTTPError):
        return 0


async def emit(pool: Any, order_id: int, event: str, data: dict[str, Any]) -> int | None:
    """Send one event for an order to its owner's address, if they set one. Never raises."""
    try:
        hook = await pool.fetchrow("SELECT url, secret FROM automation_hooks WHERE order_id = $1", order_id)
        if hook is None:
            return None
        status = await deliver(hook["url"], hook["secret"], {
            "event": event, "order_id": order_id, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "data": data})
        await pool.execute("UPDATE automation_hooks SET last_status = $2, last_at = now() WHERE order_id = $1", order_id, status)
        return status
    except Exception:  # noqa: BLE001 — a client's automation must never break the product's own form
        return None
