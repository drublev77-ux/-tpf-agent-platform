"""Screenshots of a built site, so that a model can look at it the way a visitor would.

Each page is opened in a real browser at a desktop and a phone size. Besides the picture, the browser
reports facts no picture is needed for: content wider than the phone screen, images that did not load,
script errors."""
from __future__ import annotations

from dataclasses import dataclass, field

VIEWPORTS: dict[str, tuple[int, int, int]] = {"desktop": (1280, 800, 3200), "phone": (390, 844, 3400)}   # w, h, max shot height
MAX_ROUTES = 6

FACTS_JS = """() => {
  const d = document.documentElement;
  const broken = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.currentSrc)
    .map(i => i.currentSrc.slice(0, 160));
  const wide = [];
  if (d.scrollWidth - d.clientWidth > 8) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > d.clientWidth + 8) {
        const cls = typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.') : '';
        wide.push(el.tagName.toLowerCase() + cls);
        if (wide.length >= 4) break;
      }
    }
  }
  return { overflow: d.scrollWidth - d.clientWidth, height: d.scrollHeight, broken: broken.slice(0, 4), wide,
           text: (document.body.innerText || '').trim().length };
}"""


class ScreensUnavailable(RuntimeError):
    pass


@dataclass
class Shot:
    route: str
    viewport: str
    jpeg: bytes
    defects: list[str] = field(default_factory=list)   # measured, certain
    notes: list[str] = field(default_factory=list)     # for the model to weigh


def pick_routes(routes: list[str]) -> list[str]:
    """The pages worth looking at: the home page, the other public pages, then the owner's panel."""
    plain = [r for r in dict.fromkeys(routes) if "[" not in r and r not in ("/admin/login", "/login") and not r.startswith("/api")]
    public = sorted((r for r in plain if not r.startswith("/admin")), key=lambda r: (r != "/", r.count("/"), r))
    admin = sorted((r for r in plain if r.startswith("/admin")), key=lambda r: (r != "/admin", r))
    keep_admin = admin[:2] if len(public) <= MAX_ROUTES - 2 else admin[:1]
    return public[:MAX_ROUTES - len(keep_admin)] + keep_admin


class Screenshooter:
    async def capture(self, url: str, routes: list[str], admin_cookie: str = "", user_cookie: str = "") -> list[Shot]:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ScreensUnavailable("playwright is not installed") from e
        base = url.rstrip("/")
        shots: list[Shot] = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    for name, (width, height, cap) in VIEWPORTS.items():
                        context = await browser.new_context(
                            viewport={"width": width, "height": height}, device_scale_factor=1,
                            is_mobile=name == "phone", has_touch=name == "phone", reduced_motion="reduce")
                        held = [("tpf_admin", admin_cookie), ("tpf_user", user_cookie)]
                        if admin_cookie or user_cookie:
                            await context.add_cookies([{"name": n, "value": v, "url": base} for n, v in held if v])
                        for route in routes:
                            shots.append(await self._shoot(context, base, route, name, cap, PlaywrightError))
                        await context.close()
                finally:
                    await browser.close()
        except PlaywrightError as e:
            raise ScreensUnavailable(str(e).splitlines()[0][:300]) from e
        return shots

    async def _shoot(self, context, base: str, route: str, viewport: str, cap: int, error_type) -> Shot:  # noqa: ANN001
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:200]))
        try:
            try:
                await page.goto(base + route, wait_until="networkidle", timeout=30_000)
            except error_type:
                await page.wait_for_load_state("load", timeout=15_000)    # a page that keeps a connection open
            # Walk down the page so that content shown on scroll is there, then return to the top.
            await page.evaluate("""async () => { const h = document.documentElement.scrollHeight;
                for (let y = 0; y < h; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
                window.scrollTo(0, 0); await new Promise(r => setTimeout(r, 250)); }""")
            facts = await page.evaluate(FACTS_JS)
            # A whole-page picture draws a bar fixed to the screen where the first screen ends: in the middle of the
            # page, on top of whatever is there. It was reported as "the menu covers the content" round after round.
            # Put such bars where a visitor meets them at the end of the scroll: on the last screen of the page.
            await page.evaluate("""() => { const vh = window.innerHeight, doc = document.documentElement.scrollHeight;
                if (doc <= vh + 4) return;
                for (const el of document.querySelectorAll('body *')) {
                  if (getComputedStyle(el).position !== 'fixed') continue;
                  const r = el.getBoundingClientRect();
                  if (r.height === 0 || r.top < vh / 2 || r.height > vh / 2) continue;   // only what hugs the bottom edge
                  el.style.setProperty('position', 'absolute', 'important');
                  el.style.setProperty('top', (doc - (vh - r.top)) + 'px', 'important');
                  el.style.setProperty('bottom', 'auto', 'important'); } }""")
            width = page.viewport_size["width"]
            jpeg = await page.screenshot(type="jpeg", quality=70, full_page=True,
                                         clip={"x": 0, "y": 0, "width": width, "height": min(max(facts["height"], 200), cap)})
        finally:
            await page.close()
        shot = Shot(route, viewport, jpeg)
        if facts["overflow"] > 8:
            shot.defects.append(f"the page is {facts['overflow']}px wider than the {viewport} screen, so it scrolls sideways"
                                + (f"; sticking out: {', '.join(facts['wide'])}" if facts["wide"] else ""))
        for src in facts["broken"]:
            shot.defects.append(f"image does not load: {src}")
        if facts["text"] < 20:
            shot.defects.append("the page shows almost no text")
        if facts["height"] > cap:
            shot.notes.append(f"the page is {facts['height']}px tall; the picture shows its first {cap}px")
        shot.notes += [f"script error: {e}" for e in errors[:3]]
        return shot
