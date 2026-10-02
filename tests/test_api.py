"""HTTP API: intake, client auth by order token, state guards, operator auth."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tpf.config import get_settings


@pytest.fixture
def client(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_intake_and_client_access(client):
    r = client.post("/orders", json={
        "client_name": "Li Wei", "client_email": "li@example.com", "language": "zh",
        "request_text": "我们需要一个咖啡店的落地页，包含菜单和地址。",
    })
    assert r.status_code == 201
    oid, token = r.json()["order"]["id"], r.json()["client_token"]
    assert r.json()["order"]["state"] == "spec"

    assert client.get(f"/orders/{oid}", headers={"X-Order-Token": token}).status_code == 200
    assert client.get(f"/orders/{oid}", headers={"X-Order-Token": "wrong"}).status_code == 404

    # Approving an estimate that does not exist yet is refused, not silently accepted.
    r = client.post(f"/orders/{oid}/estimate", headers={"X-Order-Token": token}, json={"approved": True})
    assert r.status_code == 409


def test_operator_endpoints_need_the_admin_token(client):
    assert client.get("/admin/orders", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.get("/admin/orders", headers={"X-Admin-Token": "test-admin"}).status_code == 200


def test_stripe_webhook_requires_a_valid_signature(client, monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    from tpf.config import get_settings
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    get_settings.cache_clear()
    body = json.dumps({"type": "checkout.session.completed",
                       "data": {"object": {"id": "cs_unknown", "payment_status": "paid", "metadata": {"stage": "prepayment"}}}}).encode()
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert r.status_code == 200                               # signed, unknown session: acknowledged, nothing moves
    assert client.get("/pay/done?order=7").status_code == 200


def test_validation_rejects_bad_input(client):
    r = client.post("/orders", json={"client_name": "A", "client_email": "not-an-email", "request_text": "short"})
    assert r.status_code == 422


def test_public_site_and_intake_limit(client, monkeypatch):
    page = client.get("/")
    assert page.status_code == 200 and "TPF" in page.text and "fetch('/orders'" in page.text
    assert client.get("/brand.jpg").headers["content-type"] == "image/jpeg"
    monkeypatch.setenv("INTAKE_HOURLY_LIMIT", "1")
    get_settings.cache_clear()
    order = {"client_name": "A", "client_email": "a@example.com", "request_text": "Нужен лендинг для кофейни."}
    assert client.post("/orders", json=order).status_code == 201
    assert client.post("/orders", json=order).status_code == 429
