"""Pictures for client sites: the manifest the developer writes, drawing with caching, placeholders, delivery."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from tpf.tools.imagegen import PLACEHOLDER_JPEG, Seedream, check_manifest, manifest, with_images
from tpf.tools.template import materialize, rules_for

PROMPT = ("A warm sunlit corner of a small neighbourhood coffee shop, wooden counter, ceramic cups, soft morning "
          "light through a large window, muted green and cream tones, shot at eye level, shallow depth of field")


def _code(images, page='<img src="/gen/hero.jpg" alt="Кофейня" />'):
    files = [{"path": "app/page.tsx", "content": f"export default function P() {{ return {page}; }}"}]
    if images is not None:
        files.append({"path": "lib/images.json", "content": json.dumps({"images": images})})
    return files


HERO = {"file": "hero.jpg", "aspect": "16:9", "alt": "Кофейня утром", "prompt": PROMPT}


def test_manifest_checks():
    assert check_manifest(_code([HERO])) == []
    assert check_manifest(_code(None, page="<p/>")) == []
    bad = check_manifest(_code([{"file": "Hero.PNG", "aspect": "5:4", "alt": "", "prompt": "cup"}]))
    assert any("bad file name" in p for p in bad) and any("aspect" in p for p in bad)
    assert any("prompt" in p for p in bad) and any("alt" in p for p in bad)
    assert any("not listed" in p for p in check_manifest(_code([], page='<img src="/gen/x.jpg" alt="" />')))
    assert any("no page shows" in p for p in check_manifest(_code([HERO], page="<p/>")))
    assert any("not valid JSON" in p for p in check_manifest([{"path": "lib/images.json", "content": "{"}]))
    assert check_manifest([{"path": "public/gen/a.jpg", "content": "x"}])
    assert "lib/images.json" in rules_for("site") and "lib/images.json" not in rules_for("bot")


class FakeModel:
    model = "fake-seedream"

    def __init__(self):
        self.calls = []

    async def draw(self, prompt, aspect):
        self.calls.append((prompt, aspect))
        return b"\xff\xd8JPEG-" + aspect.encode()


async def _ctx(env, images):
    order = await env.orch.create_order(client_name="A", client_email="a@example.com", request_text="Сайт кофейни")
    return SimpleNamespace(orch=env.orch, order_id=order[0]["id"], settings=env.settings, images=images)


async def test_pictures_are_drawn_once_and_reused(env):
    model = FakeModel()
    ctx = await _ctx(env, model)
    files = _code([HERO, {**HERO, "file": "cup.jpg", "aspect": "1:1"}], page='<img src="/gen/hero.jpg"/><img src="/gen/cup.jpg"/>')
    out = await with_images(ctx, files)
    pics = {f["path"]: f for f in out if f["path"].startswith("public/gen/")}
    assert set(pics) == {"public/gen/hero.jpg", "public/gen/cup.jpg"}
    assert base64.b64decode(pics["public/gen/cup.jpg"]["content"]) == b"\xff\xd8JPEG-1:1"
    assert all(p["encoding"] == "base64" for p in pics.values())
    await with_images(ctx, files)                   # a rebuild with the same prompts costs nothing
    assert len(model.calls) == 2
    changed = _code([{**HERO, "prompt": PROMPT + ", evening"}])
    await with_images(ctx, changed)
    assert len(model.calls) == 3


async def test_placeholder_without_key_and_past_limit(env):
    ctx = await _ctx(env, None)
    out = await with_images(ctx, _code([HERO]))
    assert base64.b64decode(out[-1]["content"]) == PLACEHOLDER_JPEG
    names = [r["name"] for r in await env.pool.fetch("SELECT name FROM events WHERE order_id=$1", ctx.order_id)]
    assert "images_placeholder" in names

    model = FakeModel()
    ctx = await _ctx(env, model)
    ctx.settings = env.settings.model_copy(update={"image_gen_per_order": 1})
    files = _code([HERO, {**HERO, "file": "b.jpg", "aspect": "4:3"}], page='<img src="/gen/hero.jpg"/><img src="/gen/b.jpg"/>')
    out = await with_images(ctx, files)
    assert len(model.calls) == 1 and base64.b64decode(out[-1]["content"]) == PLACEHOLDER_JPEG


def test_pictures_are_written_as_bytes(tmp_path):
    files = _code([HERO]) + [{"path": "public/gen/hero.jpg", "content": base64.b64encode(b"\xff\xd8x").decode(), "encoding": "base64"}]
    root = materialize(tmp_path / "w", files)
    assert (root / "public/gen/hero.jpg").read_bytes() == b"\xff\xd8x"
    assert manifest(files)[0]["file"] == "hero.jpg"


async def test_seedream_request(settings):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"img").decode()}]})

    model = Seedream(settings.model_copy(update={"seedream_api_key": "ark-test"}))
    model.client = httpx.AsyncClient(base_url="https://ark.test/api/v3", transport=httpx.MockTransport(handler),
                                     headers=model.client.headers)
    assert await model.draw(PROMPT, "16:9") == b"img"
    assert seen["auth"] == "Bearer ark-test"
    assert seen["body"]["size"] == "1664x936" and seen["body"]["model"] == settings.seedream_model
    assert seen["body"]["watermark"] is False and seen["body"]["response_format"] == "b64_json"


@pytest.mark.parametrize("encoding", [None, "base64"])
async def test_deployer_marks_binary_files(settings, encoding):
    from tpf.tools.deploy import VercelDeployer
    sent = {}

    def handler(request: httpx.Request):
        if request.url.path == "/v13/deployments":
            sent["files"] = json.loads(request.content)["files"]
            return httpx.Response(200, json={"id": "d1"})
        return httpx.Response(200, json={})

    d = VercelDeployer(settings.model_copy(update={"vercel_token": "t"}))
    d.client = httpx.AsyncClient(base_url=d.API, transport=httpx.MockTransport(handler))

    async def ready(_id, timeout_sec=1200):
        return None
    d._wait = ready
    f = {"path": "public/gen/a.jpg", "content": "AAA"} | ({"encoding": encoding} if encoding else {})
    await d.deploy("p", [f], production=False)
    assert sent["files"][0].get("encoding") == encoding
