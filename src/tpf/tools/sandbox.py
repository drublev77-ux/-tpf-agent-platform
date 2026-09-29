"""Isolated execution of builds and tests.

DockerSandbox copies the workspace into a fresh container (no host bind mounts, so it works when the
worker itself runs in a container), runs one command with CPU, memory and process limits, and throws
the container away. LocalSandbox runs the command directly: development only.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tpf.config import Settings

OUTPUT_TAIL = 20_000


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    async def run(self, workdir: Path, command: str, timeout: int | None = None) -> RunResult: ...


async def _exec(*args: str, cwd: Path | None = None, timeout: int, env: dict[str, str] | None = None) -> RunResult:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(-1, "timed out", timed_out=True)
    return RunResult(proc.returncode or 0, out.decode("utf-8", "replace")[-OUTPUT_TAIL:])


class DockerSandbox:
    def __init__(self, settings: Settings) -> None:
        self.image = settings.sandbox_image
        self.timeout = settings.sandbox_timeout_sec

    async def run(self, workdir: Path, command: str, timeout: int | None = None) -> RunResult:
        name = f"tpf-sbx-{uuid.uuid4().hex[:12]}"
        t = timeout or self.timeout
        created = await _exec(
            "docker", "create", "--name", name,
            "--memory", "2g", "--cpus", "2", "--pids-limit", "512",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--workdir", "/work", self.image, "sh", "-c", command,
            timeout=60,
        )
        if not created.ok:
            return RunResult(created.exit_code, "docker create failed: " + created.output)
        try:
            copied = await _exec("docker", "cp", f"{workdir}/.", f"{name}:/work", timeout=120)
            if not copied.ok:
                return RunResult(copied.exit_code, "docker cp failed: " + copied.output)
            result = await _exec("docker", "start", "--attach", name, timeout=t)
            if result.timed_out:
                await _exec("docker", "kill", name, timeout=30)
            return result
        finally:
            await _exec("docker", "rm", "-f", name, timeout=60)


class LocalSandbox:
    """No isolation. Use only on a developer machine with trusted code."""

    def __init__(self, settings: Settings) -> None:
        self.timeout = settings.sandbox_timeout_sec

    async def run(self, workdir: Path, command: str, timeout: int | None = None) -> RunResult:
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_TOKEN", "_KEY", "_SECRET"))}
        return await _exec("sh", "-c", command, cwd=workdir, timeout=timeout or self.timeout, env=env)


def make_sandbox(settings: Settings) -> Sandbox:
    return LocalSandbox(settings) if settings.sandbox == "local" else DockerSandbox(settings)


def quote(s: str) -> str:
    return shlex.quote(s)
