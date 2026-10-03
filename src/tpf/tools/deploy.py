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
