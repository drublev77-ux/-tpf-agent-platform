"""Pictures for client sites, drawn by Seedream 5.0 Pro (ByteDance, through BytePlus ModelArk).

The developer does not write image files. It lists the pictures a site needs in lib/images.json, each with a
prompt and an aspect ratio, and refers to them as /gen/<file> in the pages. Before a build the platform draws
every listed picture once, keeps it in the platform database (generated_images) and adds it to the deployment
as public/gen/<file>. A picture whose prompt and aspect did not change is not drawn again, so later builds of
the same order cost nothing. Without SEEDREAM_API_KEY, or past the per-order limit, a picture is a neutral grey
placeholder: the site still builds and the operator sees why in the order's log.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Protocol

import httpx

from tpf.config import Settings

MANIFEST_PATH = "lib/images.json"
PUBLIC_DIR = "public/gen/"
MAX_IMAGES = 12
ASPECTS = {     # about 1.5 megapixels: inside the cheaper price tier (up to 2.36 MP), sharp on a wide screen
    "16:9": (1664, 936), "21:9": (1904, 816), "3:2": (1536, 1024), "4:3": (1440, 1080), "1:1": (1248, 1248),
    "3:4": (1080, 1440), "2:3": (1024, 1536), "9:16": (936, 1664),
}
_FILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.jpg$")
# A 16x9 grey JPEG: what a picture shows while it cannot be drawn
PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5E"
    "SUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAAR"
    "CAAJABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEG"
    "E1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEB"
    "AQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0"
    "tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD02iiipGf/2Q=="
)

IMAGES_RULES = (
    "PICTURES: the platform draws pictures for the site with an image model. Do not link photos from other sites, "
    "stock libraries or placeholder services, and do not write image files yourself. When a page needs a picture "
    "(a hero, a section illustration, a service or product card) and the client has not supplied one, list it in "
    f"{MANIFEST_PATH}: {{\"images\": [{{\"file\": \"hero.jpg\", \"aspect\": \"16:9\", \"alt\": \"…\", \"prompt\": \"…\"}}]}} "
    f"— at most {MAX_IMAGES} pictures; file is lowercase latin letters, digits and dashes ending in .jpg; aspect is "
    f"one of {', '.join(ASPECTS)}; alt is the picture's alt text in the site's language; prompt, in English, "
    "describes one photograph or illustration in 30–80 words: subject, setting, light, mood, colours that suit the "
    "site's palette, camera angle. A prompt never asks for text, letters, logos, brand names, watermarks or "
    "recognisable real people, and never presents the client's own premises, staff or products as real: show the "
    "kind of place, work or product in general. Use the picture as <img src=\"/gen/hero.jpg\" alt=\"…\" "
    "width=… height=… loading=\"lazy\"> (no loading=\"lazy\" on the first screen) with the same alt, styled with "
    "object-fit: cover so any picture fits its box. Pictures the client supplied as links are used as they are. "
    "A site that needs no pictures has no lib/images.json. Never write files under public/gen/."
)


def manifest(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """The pictures the code asks for (an unreadable manifest asks for none; check_manifest reports it)."""
    raw = next((f["content"] for f in files if f["path"] == MANIFEST_PATH), None)
    if raw is None:
        return []
    try:
        items = json.loads(raw).get("images") or []
    except (ValueError, AttributeError):
        return []
    return [i for i in items if isinstance(i, dict)][:MAX_IMAGES]


def check_manifest(files: list[dict[str, str]]) -> list[str]:
    """Static checks of lib/images.json, run with the developer's other checks."""
    problems: list[str] = []
    for f in files:
        if f["path"].startswith(PUBLIC_DIR):
            problems.append(f"{f['path']}: files under {PUBLIC_DIR} are drawn by the platform from {MANIFEST_PATH}")
    raw = next((f["content"] for f in files if f["path"] == MANIFEST_PATH), None)
    if raw is None:
        return problems
    try:
        data = json.loads(raw)
        items = data.get("images") if isinstance(data, dict) else None
    except ValueError as e:
        return problems + [f"{MANIFEST_PATH} is not valid JSON: {e}"]
    if not isinstance(items, list):
        return problems + [f'{MANIFEST_PATH} must be {{"images": [...]}}']
    if len(items) > MAX_IMAGES:
        problems.append(f"{MANIFEST_PATH} lists {len(items)} pictures; at most {MAX_IMAGES}")
    seen: set[str] = set()
    for n, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"{MANIFEST_PATH}: picture {n + 1} is not an object")
            continue
        name = str(item.get("file", ""))
        if not _FILE.match(name):
            problems.append(f"{MANIFEST_PATH}: bad file name {name!r} (lowercase latin, digits, dashes, .jpg)")
        if name in seen:
            problems.append(f"{MANIFEST_PATH}: {name} is listed twice")
        seen.add(name)
        if item.get("aspect") not in ASPECTS:
            problems.append(f"{MANIFEST_PATH}: {name} has aspect {item.get('aspect')!r}; use one of {', '.join(ASPECTS)}")
        if len(str(item.get("prompt", "")).split()) < 8:
            problems.append(f"{MANIFEST_PATH}: {name} needs a prompt of 30–80 words")
        if not str(item.get("alt", "")).strip():
            problems.append(f"{MANIFEST_PATH}: {name} needs alt text")
    used = "\n".join(f["content"] for f in files if f["path"].endswith((".tsx", ".ts", ".css")))
    for name in seen:
        if f"/gen/{name}" not in used:
            problems.append(f"{MANIFEST_PATH} lists {name}, but no page shows /gen/{name}")
    for name in set(re.findall(r"/gen/([A-Za-z0-9._-]+)", used)) - seen:
        problems.append(f"/gen/{name} is used in the code but not listed in {MANIFEST_PATH}")
    return problems


class ImageError(RuntimeError):
    pass


class ImageModel(Protocol):
    model: str

    async def draw(self, prompt: str, aspect: str) -> bytes: ...


class Seedream:
    """BytePlus ModelArk image generation (OpenAI-style /images/generations)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.seedream_api_key:
            raise ImageError("SEEDREAM_API_KEY is not set")
        self.model = settings.seedream_model
        self.client = httpx.AsyncClient(
            base_url=settings.seedream_base_url.rstrip("/"), timeout=180,
            headers={"Authorization": f"Bearer {settings.seedream_api_key}"},
        )

    async def draw(self, prompt: str, aspect: str) -> bytes:
        w, h = ASPECTS[aspect]
        body = {"model": self.model, "prompt": prompt, "size": f"{w}x{h}", "response_format": "b64_json",
                "output_format": "jpeg", "watermark": False, "sequential_image_generation": "disabled"}
        r = await self.client.post("/images/generations", json=body)
        if r.status_code >= 400:
            raise ImageError(f"Seedream {r.status_code}: {r.text[:400]}")
        data = (r.json().get("data") or [{}])[0]
        if data.get("b64_json"):
            return base64.b64decode(data["b64_json"])
        if data.get("url"):           # a provider that ignores response_format: the link lives for a day
            img = await self.client.get(data["url"], headers={"Authorization": ""})
            img.raise_for_status()
            return img.content
        raise ImageError(f"Seedream returned no image: {str(r.json())[:300]}")


def make_image_model(settings: Settings) -> ImageModel | None:
    return Seedream(settings) if settings.seedream_api_key else None


def _key(model: str, item: dict[str, Any]) -> str:
    return hashlib.sha256(f"{model}\n{item.get('aspect')}\n{item.get('prompt')}".encode()).hexdigest()


async def with_images(ctx: Any, files: list[dict[str, str]]) -> list[dict[str, str]]:
    """The code's files plus the pictures its manifest lists, as base64 files under public/gen/."""
    items = manifest(files)
    if not items:
        return files
    pool, order_id = ctx.orch.pool, ctx.order_id
    model: ImageModel | None = getattr(ctx, "images", None)
    model_name = model.model if model else "placeholder"
    drawn = await pool.fetchval("SELECT count(*) FROM generated_images WHERE order_id = $1", order_id)
    out = list(files)
    missing: list[str] = []
    for item in items:
        name = str(item.get("file", ""))
        if not _FILE.match(name) or item.get("aspect") not in ASPECTS:
            continue
        key = _key(model_name, item)
        data = await pool.fetchval("SELECT data FROM generated_images WHERE order_id = $1 AND key = $2", order_id, key)
        if data is None and model is not None and drawn < ctx.settings.image_gen_per_order:
            try:
                data = await model.draw(str(item["prompt"]), item["aspect"])
            except (ImageError, httpx.HTTPError) as e:
                await ctx.orch.log(order_id, "image_failed", "images", {"file": name, "error": str(e)[:500]}, kind="tool")
            else:
                drawn += 1
                await pool.execute(
                    "INSERT INTO generated_images (order_id, key, file, prompt, aspect, model, data) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (order_id, key) DO NOTHING",
                    order_id, key, name, str(item["prompt"]), item["aspect"], model_name, data)
                await ctx.orch.log(order_id, "image_generated", "images",
                                   {"file": name, "model": model_name, "bytes": len(data)}, kind="tool")
        if data is None:
            missing.append(name)
            data = PLACEHOLDER_JPEG
        out.append({"path": PUBLIC_DIR + name, "content": base64.b64encode(data).decode(), "encoding": "base64"})
    if missing:
        why = ("SEEDREAM_API_KEY не задан" if model is None
               else f"лимит {ctx.settings.image_gen_per_order} картинок на заказ или ошибка модели")
        await ctx.orch.log(order_id, "images_placeholder", "images", {"files": missing, "reason": why}, kind="tool")
    return out
