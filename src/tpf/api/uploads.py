"""A video attached to a new order. The site sends the file in pieces small enough for the host's request
limit; the pieces wait in the database until a worker has watched the video, then they are deleted.

Anyone can open the order form, so the limits here are what stands between the form and a full disk."""
from __future__ import annotations

import hmac
import math
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(include_in_schema=False)

MAX_SIZE = 50 * 1024 * 1024          # one video
CHUNK = 3 * 1024 * 1024              # the host accepts request bodies up to 4.5 MB
MAX_WAITING = 300 * 1024 * 1024      # all videos not yet watched, together
PER_IP_HOUR = 4
EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v", ".mkv", ".3gp", ".avi")


class UploadIn(BaseModel):
    size: int = Field(gt=0, le=MAX_SIZE)
    filename: str = Field(default="", max_length=200)


def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else ""))[:60]


@router.post("/uploads", status_code=201)
async def start_upload(body: UploadIn, request: Request) -> dict[str, Any]:
    if not body.filename.lower().endswith(EXTENSIONS):
        raise HTTPException(415, "a video file is expected")
    pool = request.app.state.orch.pool
    # Uploads that were never attached to an order, or were abandoned half-way, do not stay.
    await pool.execute("DELETE FROM video_uploads WHERE order_id IS NULL AND created_at < now() - interval '1 hour'")
    ip = _ip(request)
    mine = await pool.fetchval(
        "SELECT count(*) FROM video_uploads WHERE ip = $1 AND created_at > now() - interval '1 hour'", ip)
    waiting = await pool.fetchval(
        "SELECT coalesce(sum(size), 0) FROM video_uploads WHERE status IN ('uploading', 'complete')")
    if mine >= PER_IP_HOUR or waiting + body.size > MAX_WAITING:
        raise HTTPException(429, "too many videos right now, try again later")
    token = secrets.token_urlsafe(24)
    upload_id = await pool.fetchval(
        "INSERT INTO video_uploads (token, filename, size, ip) VALUES ($1, $2, $3, $4) RETURNING id",
        token, body.filename, body.size, ip)
    return {"id": upload_id, "token": token, "chunk_size": CHUNK, "chunks": math.ceil(body.size / CHUNK)}


@router.put("/uploads/{upload_id}/chunks/{n}")
async def put_chunk(upload_id: int, n: int, request: Request, x_upload_token: str = Header(...)) -> dict[str, Any]:
    pool = request.app.state.orch.pool
    up = await pool.fetchrow("SELECT token, size, status FROM video_uploads WHERE id = $1", upload_id)
    if up is None or not hmac.compare_digest(up["token"], x_upload_token):
        raise HTTPException(404, "upload not found")
    total = math.ceil(up["size"] / CHUNK)
    if up["status"] != "uploading" or not 0 <= n < total:
        raise HTTPException(409, "this piece is not expected")
    data = await request.body()
    expected = CHUNK if n < total - 1 else up["size"] - CHUNK * (total - 1)
    if len(data) != expected:
        raise HTTPException(400, "wrong piece size")
    await pool.execute("INSERT INTO video_chunks (upload_id, n, data) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", upload_id, n, data)
    received = await pool.fetchval("SELECT coalesce(sum(length(data)), 0) FROM video_chunks WHERE upload_id = $1", upload_id)
    done = received == up["size"]
    await pool.execute("UPDATE video_uploads SET received = $2, status = $3 WHERE id = $1 AND status = 'uploading'",
                       upload_id, received, "complete" if done else "uploading")
    return {"received": received, "complete": done}


async def claim(pool: Any, upload_id: int, token: str) -> bool:
    """True when this finished upload exists, belongs to nobody yet and the caller knows its token."""
    up = await pool.fetchrow("SELECT token, status, order_id FROM video_uploads WHERE id = $1", upload_id)
    return bool(up and up["order_id"] is None and up["status"] == "complete" and hmac.compare_digest(up["token"], token))
