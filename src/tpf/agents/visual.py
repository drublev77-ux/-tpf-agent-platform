"""Looking at the built site: screenshots of its pages at a desktop and a phone size, judged by a model."""
from __future__ import annotations

import base64
import logging
from typing import Any

from tpf.agents.base import Ctx
from tpf.tools.screens import ScreensUnavailable, pick_routes

log = logging.getLogger("tpf.visual")

SYSTEM = (
    "You are a QA engineer looking at screenshots of a website that was just built for a client, before the client "
    "sees it. Each screenshot is labelled with its page and screen size (desktop 1280px wide, phone 390px wide). "
    "Report what a visitor would see as broken, and nothing else. "
    "severity 'broken' — the page cannot go to a client like this: text or elements overlapping each other; text cut "
    "off, running out of its box or off the screen; a page or large block rendered without styles; text that cannot "
    "be read against its background; a large empty area where content clearly belongs; a layout that falls apart on "
    "the phone (columns squeezed until words break letter by letter, controls too small or off-screen); a visible "
    "error message, stack trace, raw code, 'undefined', 'NaN', '[object Object]' or untranslated key; a menu, button "
    "or form that is visibly unusable. "
    "severity 'minor' — real but small: uneven spacing, slight misalignment, a heading that wraps awkwardly. "
    "Do NOT report: matters of taste (colours, fonts, wording, how modern it looks); text in [square brackets], "
    "which is an intentional placeholder for content the client has not supplied; empty lists and zero counters in "
    "the admin area (the database is new); the picture ending mid-page when a note says it was cut; anything you "
    "cannot actually see in a screenshot. When a note lists a script error, mention it only if the page visibly "
    "suffers. For each problem give the page, the screen size, what is wrong and where on the page, and what to "
    "change in the layout or styles. If everything is fine, return an empty list."
)

SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["issues"], "additionalProperties": False,
    "properties": {"issues": {"type": "array", "maxItems": 20, "items": {
        "type": "object", "required": ["route", "viewport", "severity", "problem", "fix"], "additionalProperties": False,
        "properties": {
            "route": {"type": "string"}, "viewport": {"type": "string", "enum": ["desktop", "phone"]},
            "severity": {"type": "string", "enum": ["broken", "minor"]},
            "problem": {"type": "string"}, "fix": {"type": "string"}}}}},
}


async def look(ctx: Ctx, url: str, routes: list[str], cookie: str = "", about: str = "", user_cookie: str = "") -> dict[str, Any]:
    """What is visibly wrong with the site at `url`. Never raises: a check that could not run says so."""
    if ctx.screens is None:
        return {"ok": True, "skipped": "screenshots are not available on this worker"}
    chosen = pick_routes(routes) or ["/"]
    try:
        shots = await (ctx.screens.capture(url, chosen, cookie, user_cookie) if user_cookie else ctx.screens.capture(url, chosen, cookie))
    except ScreensUnavailable as e:
        log.warning("order %s: no screenshots: %s", ctx.order_id, e)
        return {"ok": True, "skipped": f"screenshots failed: {e}"}
    measured = [f"{s.route} ({s.viewport}): {d}" for s in shots for d in s.defects]
    images = [{"label": f"Page {s.route} — {s.viewport}" + ("".join(f"\nNote: {n}" for n in s.notes + s.defects)),
               "media_type": "image/jpeg", "data": base64.b64encode(s.jpeg).decode()} for s in shots]
    result = await ctx.llm.structured(
        order_id=ctx.order_id, agent="visual", model_class="reviewer", system=SYSTEM, schema=SCHEMA, max_tokens=4000,
        prompt=(f"{about}\n\n" if about else "") + f"{len(shots)} screenshots follow. Report what is visibly broken.",
        images=images)
    seen = [i for i in result["issues"] if isinstance(i, dict)]
    line = lambda i: f"{i['route']} ({i['viewport']}): {i['problem']} — fix: {i['fix']}"   # noqa: E731
    broken = measured + [line(i) for i in seen if i["severity"] == "broken"]
    minor = [line(i) for i in seen if i["severity"] != "broken"]
    await ctx.orch.log(ctx.order_id, "visual_check", "qa", {
        "url": url, "pages": chosen, "broken": broken, "minor": minor}, kind="tool")
    return {"ok": not broken, "issues": broken, "minor": minor, "pages": chosen}


async def check_live(ctx: Ctx) -> Any:
    """Side task: look at the order's live (or review) site as it is now and record what is seen."""
    from tpf.agents.base import Outcome
    from tpf.agents.qa import admin_cookie
    from tpf.tools.template import has_database, kind_of

    url = ctx.order.get("production_url") or ctx.order.get("staging_url")
    arch = await ctx.orch.latest_artifact(ctx.order_id, "architecture")
    spec = await ctx.orch.latest_artifact(ctx.order_id, "spec")
    if not url or not arch:
        return Outcome(data={"skipped": "nothing is deployed"})
    cookie = ""
    if spec and has_database(kind_of(spec)) and ctx.tenant_db is not None:
        db = await ctx.tenant_db.ensure(ctx.orch.pool, ctx.order_id, "prod" if ctx.order.get("production_url") else "stg")
        cookie = admin_cookie(db.session_secret)
    return Outcome(data=await look(ctx, url, [r["path"] for r in arch["routes"]], cookie))
