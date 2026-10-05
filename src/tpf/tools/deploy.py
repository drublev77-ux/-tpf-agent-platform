"""Deployment to Vercel through its REST API: files are uploaded inline, Vercel builds the site.

Vercel's build runs in its own isolated environment, so a preview deployment doubles as the
QA build check when the worker has no Docker (SANDBOX=vercel): a failed build comes back as a
DeployError carrying the build log.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from tpf.config import Settings


@dataclass
class Deployment:
    id: str
    url: str
    state: str


class DeployError(RuntimeError):
    def __init__(self, message: str, log: str = "", deployment_id: str | None = None) -> None:
        super().__init__(message)
        self.log = log
        self.deployment_id = deployment_id


class Deployer(Protocol):
    async def deploy(
        self, project: str, files: list[dict[str, str]], production: bool, build_command: str | None = None,
    ) -> Deployment: ...


class VercelDeployer:
    API = "https://api.vercel.com"

    def __init__(self, settings: Settings) -> None:
        if not settings.vercel_token:
            raise DeployError("VERCEL_TOKEN is not set")
        # A token scoped to a team is refused without the team; accept its id (team_…) or its slug.
        team = settings.vercel_team_id.strip()
        self.params = ({"teamId": team} if team.startswith("team_") else {"slug": team}) if team else {}
        self._public: set[str] = set()
        self.client = httpx.AsyncClient(
            base_url=self.API, timeout=120,
            headers={"Authorization": f"Bearer {settings.vercel_token}"},
        )

    async def deploy(
        self, project: str, files: list[dict[str, str]], production: bool, build_command: str | None = None,
    ) -> Deployment:
        settings: dict = {"framework": "nextjs"}
        if build_command:
            settings |= {"buildCommand": build_command, "installCommand": "npm install --no-audit --no-fund"}
        body = {
            "name": project,
            "files": [{"file": f["path"], "data": f["content"]} for f in files],
            "projectSettings": settings,
        }
        if production:
            body["target"] = "production"
        r = await self.client.post("/v13/deployments", params=self.params | {"skipAutoDetectionConfirmation": "1"}, json=body)
        if r.status_code >= 400:
            raise DeployError(f"Vercel {r.status_code}: {r.text[:500]}")
        await self._make_public(project)
        return await self._wait(r.json()["id"])

    async def set_env(self, project: str, env: dict[str, str]) -> None:
        """Create the project if needed and set its environment variables (kept encrypted by Vercel)."""
        r = await self.client.post("/v11/projects", params=self.params, json={"name": project, "framework": "nextjs"})
        if r.status_code >= 400 and r.status_code != 409:   # 409: the project already exists
            raise DeployError(f"Vercel {r.status_code}: {r.text[:500]}")
        body = [{"key": k, "value": v, "type": "encrypted", "target": ["production", "preview"]} for k, v in env.items()]
        r = await self.client.post(f"/v10/projects/{project}/env", params=self.params | {"upsert": "true"}, json=body)
        if r.status_code >= 400:
            raise DeployError(f"Vercel {r.status_code}: {r.text[:500]}")

    async def health(self, url: str, path: str = "/api/tpf-health") -> str | None:
        """None when the deployed application answers that its database is reachable, else what went wrong."""
        last = "no answer"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            for _ in range(4):
                try:
                    r = await http.get(url.rstrip("/") + path)
                    if r.status_code == 200 and r.json().get("db") == "ok":
                        return None
                    last = f"{path} answered {r.status_code}: {r.text[:200]}"
                except (httpx.HTTPError, ValueError) as e:
                    last = f"{path}: {type(e).__name__}: {e}"
                await asyncio.sleep(4)
        return last

    async def smoke(self, url: str, routes: list[str], admin_cookie: str = "") -> list[str]:
        """Open every page of a deployed application the way a browser would, signed in as the owner, and
        return what is broken: a page that answers with a server error or does not answer at all."""
        problems: list[str] = []
        cookies = {"tpf_admin": admin_cookie} if admin_cookie else None
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, cookies=cookies) as http:
            for route in routes:
                try:
                    r = await http.get(url.rstrip("/") + route)
                except httpx.HTTPError as e:
                    problems.append(f"{route}: no answer ({type(e).__name__})")
                    continue
                if r.status_code >= 500:
                    problems.append(f"{route}: server error {r.status_code}")
                elif route.startswith("/admin") and route != "/admin/login" and r.status_code in (301, 302, 303, 307, 308):
                    problems.append(f"{route}: redirects to {r.headers.get('location', '?')} although the owner is signed in")
        return problems

    async def smoke_access(self, url: str, cases: list[tuple[str, dict[str, str], bool, str]]) -> list[str]:
        """Who can open what. Each case is (route, cookies, should it open, who is asking). A page that should
        open must answer 2xx; one that should not must turn the visitor away (a redirect, 401, 403 or 404)."""
        problems: list[str] = []
        for route, cookies, opens, who in cases:
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=False, cookies=cookies or None) as http:
                    r = await http.get(url.rstrip("/") + route)
            except httpx.HTTPError as e:
                problems.append(f"{route} as {who}: no answer ({type(e).__name__})")
                continue
            if r.status_code >= 500:
                problems.append(f"{route} as {who}: server error {r.status_code}")
            elif opens and not 200 <= r.status_code < 300:
                problems.append(f"{route} as {who}: should open but answers {r.status_code}"
                                + (f" → {r.headers.get('location')}" if r.is_redirect else ""))
            elif not opens and 200 <= r.status_code < 300:
                problems.append(f"{route} as {who}: opens, although {who} must not see this page")
        return problems

    async def check_payments(self, url: str, token: str) -> list[str]:
        """Have a built shop run its own payment handling against throwaway orders (app/api/tpf-check).
        Returns what failed; empty when a payment of the right amount, and only that, marks an order paid."""
        path = "/api/tpf-check"
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=False) as http:
                r = await http.post(url.rstrip("/") + path, headers={"x-tpf-check": token})
            body = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as e:
            return [f"{path}: {type(e).__name__}: {e}"]
        if r.status_code != 200 or not isinstance(body.get("steps"), list):
            return [f"{path} answered {r.status_code}: {r.text[:300]}"]
        return [f"{s.get('name')}: {s.get('detail') or 'failed'}" for s in body["steps"] if not s.get("ok")]

    async def _make_public(self, project: str) -> None:
        """New Vercel projects hide previews behind a Vercel login; the client must see staging."""
        if project in self._public:
            return
        try:
            r = await self.client.patch(f"/v9/projects/{project}", params=self.params, json={"ssoProtection": None})
            if r.status_code < 400:
                self._public.add(project)
        except httpx.HTTPError:
            pass  # the deployment itself is fine; the next deploy tries again

    async def _wait(self, deployment_id: str, timeout_sec: int = 1200) -> Deployment:
        waited = 0
        while waited < timeout_sec:
            r = await self.client.get(f"/v13/deployments/{deployment_id}", params=self.params)
            r.raise_for_status()
            d = r.json()
            state = d.get("readyState") or d.get("status")
            if state == "READY":
                host = d["url"]
                if d.get("target") == "production" and d.get("alias"):
                    host = min(d["alias"], key=len)  # the stable project address, not the per-build one
                return Deployment(d["id"], "https://" + host, state)
            if state in ("ERROR", "CANCELED"):
                raise DeployError(
                    f"deployment {deployment_id} ended in {state}",
                    log=await self.build_log(deployment_id), deployment_id=deployment_id,
                )
            await asyncio.sleep(10)
            waited += 10
        raise DeployError(f"deployment {deployment_id} not ready after {timeout_sec}s", deployment_id=deployment_id)

    async def build_log(self, deployment_id: str, tail_chars: int = 8000) -> str:
        try:
            r = await self.client.get(
                f"/v3/deployments/{deployment_id}/events", params=self.params | {"builds": "1", "limit": "-1"},
            )
            r.raise_for_status()
            lines = [(e.get("payload") or {}).get("text") or e.get("text") or "" for e in r.json()]
            return "\n".join(line for line in lines if line)[-tail_chars:]
        except Exception as e:  # noqa: BLE001 — the log is a nice-to-have for the Developer agent
            return f"(build log unavailable: {e})"
