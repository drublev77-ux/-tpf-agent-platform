"""Deployment to Vercel through its REST API: files are uploaded inline, Vercel builds the site."""
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


class Deployer(Protocol):
    async def deploy(self, project: str, files: list[dict[str, str]], production: bool) -> Deployment: ...


class DeployError(RuntimeError):
    pass


class VercelDeployer:
    API = "https://api.vercel.com"

    def __init__(self, settings: Settings) -> None:
        if not settings.vercel_token:
            raise DeployError("VERCEL_TOKEN is not set")
        self.params = {"teamId": settings.vercel_team_id} if settings.vercel_team_id else {}
        self.client = httpx.AsyncClient(
            base_url=self.API, timeout=120,
            headers={"Authorization": f"Bearer {settings.vercel_token}"},
        )

    async def deploy(self, project: str, files: list[dict[str, str]], production: bool) -> Deployment:
        body = {
            "name": project,
            "files": [{"file": f["path"], "data": f["content"]} for f in files],
            "projectSettings": {"framework": "nextjs"},
        }
        if production:
            body["target"] = "production"
        r = await self.client.post("/v13/deployments", params=self.params, json=body)
        if r.status_code >= 400:
            raise DeployError(f"Vercel {r.status_code}: {r.text[:500]}")
        dep = r.json()
        return await self._wait(dep["id"])

    async def _wait(self, deployment_id: str, timeout_sec: int = 900) -> Deployment:
        waited = 0
        while waited < timeout_sec:
            r = await self.client.get(f"/v13/deployments/{deployment_id}", params=self.params)
            r.raise_for_status()
            d = r.json()
            state = d.get("readyState") or d.get("status")
            if state == "READY":
                return Deployment(d["id"], "https://" + d["url"], state)
            if state in ("ERROR", "CANCELED"):
                raise DeployError(f"deployment {deployment_id} ended in {state}")
            await asyncio.sleep(10)
            waited += 10
        raise DeployError(f"deployment {deployment_id} not ready after {timeout_sec}s")
