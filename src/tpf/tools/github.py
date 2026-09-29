"""Optional: publish each delivered order's code to its own private GitHub repository."""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx

from tpf.config import Settings
from tpf.tools.sandbox import _exec


class GitHubPublisher:
    API = "https://api.github.com"

    def __init__(self, settings: Settings) -> None:
        self.token = settings.github_token
        self.owner = settings.github_owner

    async def publish(self, repo: str, files: list[dict[str, str]], message: str) -> str:
        """Create the repo if needed and push `files` as one commit. Returns the repo URL."""
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(base_url=self.API, headers=headers, timeout=30) as gh:
            me = (await gh.get("/user")).json().get("login")
            path = "/user/repos" if me == self.owner else f"/orgs/{self.owner}/repos"
            r = await gh.post(path, json={"name": repo, "private": True, "auto_init": False})
            if r.status_code not in (201, 422):  # 422: already exists
                raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for f in files:
                p = root / f["path"]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f["content"], encoding="utf-8")
            remote = f"https://x-access-token:{self.token}@github.com/{self.owner}/{repo}.git"
            for cmd in (
                ["git", "init", "-b", "main"],
                ["git", "-c", "user.name=TPF Agents", "-c", "user.email=agents@tpf.local", "add", "-A"],
                ["git", "-c", "user.name=TPF Agents", "-c", "user.email=agents@tpf.local", "commit", "-m", message],
                ["git", "push", "--force", remote, "main"],
            ):
                res = await _exec(*cmd, cwd=root, timeout=120)
                if not res.ok:
                    raise RuntimeError(f"{cmd[1]} failed: {res.output.replace(self.token, '***')[-500:]}")
        return f"https://github.com/{self.owner}/{repo}"
