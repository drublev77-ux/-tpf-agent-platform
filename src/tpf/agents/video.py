"""A new order came with a video: watch it before the analyst starts, and put what it says into the
conversation in words. Whatever goes wrong here, the order still goes on to the analyst."""
from __future__ import annotations

import base64
import logging
import tempfile
from pathlib import Path
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome
from tpf.orchestrator.states import Ev
from tpf.tools import video

log = logging.getLogger("tpf.agents.video")

SYSTEM = (
    "A client of a web studio attached a video to their order instead of describing everything in words. You get "
    "still frames from it, in order, each labelled with its second, and the transcript of what is said (it may be "
    "empty or contain recognition mistakes). The studio builds websites, web apps with a database and an admin "
    "area, online shops and Telegram bots. Write down, for the analyst who will prepare the spec and the estimate, "
    "what the client wants built — only what the video actually shows or says. "
    "`summary`: what the client is asking for, in a few sentences. "
    "`shown`: what is on screen — the client's current site or app, somebody else's site shown as an example (name "
    "it and say what the client likes or dislikes about it if they say so), sketches, documents, a shop's shelves, "
    "a price list — with the details an analyst would need. "
    "`details`: every concrete fact and requirement: pages and features asked for, business name, texts, prices, "
    "contacts, opening hours, colours, wording that is readable in the frames or said aloud, one item each. "
    "`unclear`: what the video leaves open and the analyst should ask about, or should not assume. "
    "Do not invent anything; if the video does not describe an order at all, say so in `summary` and leave the "
    "lists empty. Write in the client's language. " + SAFETY_NOTE
)

SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["summary", "shown", "details", "unclear"], "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"}, "shown": {"type": "string"},
        "details": {"type": "array", "items": {"type": "string"}, "maxItems": 60},
        "unclear": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
}

HEAD = {
    "ru": ("Содержание вашего видео — как мы его поняли", "Что показано", "Детали", "Осталось неясным", "Сказано в видео",
           "К заявке было приложено видео, но разобрать его не удалось. Опишите, пожалуйста, задачу текстом."),
    "en": ("What your video says — as we understood it", "What is shown", "Details", "Left unclear", "Said in the video",
           "A video was attached to the request, but we could not read it. Please describe the task in text."),
    "zh": ("您的视频内容——我们的理解", "画面内容", "细节", "尚不明确", "视频中的讲话",
           "需求附带了一段视频，但我们未能解析。请用文字描述您的需求。"),
}


def as_message(brief: dict[str, Any], speech: str, language: str) -> str:
    title, shown, details, unclear, said, _ = HEAD.get(language, HEAD["en"])
    parts = [f"[{title}]", brief["summary"].strip()]
    if brief.get("shown", "").strip():
        parts.append(f"{shown}: {brief['shown'].strip()}")
    if brief.get("details"):
        parts.append(f"{details}:\n" + "\n".join(f"- {d}" for d in brief["details"]))
    if brief.get("unclear"):
        parts.append(f"{unclear}:\n" + "\n".join(f"- {d}" for d in brief["unclear"]))
    if speech:
        parts.append(f"{said}: «{speech[:4000]}»")
    return "\n\n".join(parts)[:19000]


async def _understand(ctx: Ctx, upload_id: int) -> str:
    pool = ctx.orch.pool
    with tempfile.TemporaryDirectory(prefix="tpf-video-") as tmp:
        path = Path(tmp) / "video"
        with path.open("wb") as f:
            for n in [r["n"] for r in await pool.fetch("SELECT n FROM video_chunks WHERE upload_id = $1 ORDER BY n", upload_id)]:
                f.write(await pool.fetchval("SELECT data FROM video_chunks WHERE upload_id = $1 AND n = $2", upload_id, n))
        watched = await video.watch(path, Path(tmp), ctx.order['language'])
    images = [{"label": f"Frame at {int(sec // 60)}:{int(sec % 60):02d}", "media_type": "image/jpeg",
               "data": base64.b64encode(jpeg).decode()} for sec, jpeg in watched.frames]
    brief = await ctx.llm.structured(
        order_id=ctx.order_id, agent="video", model_class="strong", system=SYSTEM, schema=SCHEMA, max_tokens=6000,
        prompt=(f"Client: {ctx.order['client_name']}\nClient language: {ctx.client_language}\n"
                f"What the client wrote in the form: {ctx.order['request_text'][:3000]}\n\n"
                f"The video is {watched.seconds:.0f} seconds long; {len(images)} frames follow.\n"
                + (f"Transcript of the speech:\n{watched.speech}" if watched.speech else f"No transcript: {watched.speech_note}.")),
        images=images)
    await ctx.orch.save_artifact(ctx.order_id, "video_brief", brief | {
        "seconds": watched.seconds, "frames": len(images), "speech": watched.speech, "speech_note": watched.speech_note}, "video")
    return as_message(brief, watched.speech, ctx.order["language"])


async def run(ctx: Ctx) -> Outcome:
    upload_id = int(ctx.payload.get("upload_id", 0))
    pool = ctx.orch.pool
    try:
        text = await _understand(ctx, upload_id)
        status = "processed"
    except Exception as e:  # noqa: BLE001 — an unreadable video must not strand the order
        log.exception("order %s: video %s was not understood", ctx.order_id, upload_id)
        await ctx.orch.log(ctx.order_id, "video_failed", "video", {"error": f"{type(e).__name__}: {e}"[:500]}, kind="tool")
        text, status = "", "failed"
    if text:
        await ctx.orch.add_message(ctx.order_id, "in", text)
    else:
        await ctx.orch.add_message(ctx.order_id, "out", HEAD.get(ctx.order["language"], HEAD["en"])[5])
    await pool.execute("DELETE FROM video_chunks WHERE upload_id = $1", upload_id)        # the file is not kept
    await pool.execute("UPDATE video_uploads SET status = $2 WHERE id = $1", upload_id, status)
    return Outcome(Ev.INTAKE_ACCEPTED, data={"video": status})
