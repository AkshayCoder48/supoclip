"""Agency fetch relay.

Lets the operator's GTA6 agency pipeline download a YouTube video through
this deployment's IP (the agency sandbox IP is bot-flagged by YouTube).

- POST /fetch-relay/download  {"url": ..., "video_id": ...}
  Header: x-relay-token: <FETCH_RELAY_TOKEN>
- Exactly ONE download runs at a time (512 MB free plan: the OOM rule).
- Uses the same yt-dlp machinery the worker uses (download_full_with_proxy),
  quality-capped by YOUTUBE_MAX_HEIGHT, no cookies are ever involved.
- The file streams back as video/mp4 and the staging dir is removed after.
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fetch-relay", tags=["fetch-relay"])

_lock = asyncio.Lock()  # OOM rule: one video at a time, everything else 429s

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,16}$")
_ALLOWED_HOSTS = ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com")


def _authorized(request: Request) -> bool:
    token = os.getenv("FETCH_RELAY_TOKEN", "")
    supplied = request.headers.get("x-relay-token", "")
    return bool(token) and secrets.compare_digest(token, supplied)


def _validate(url: str, video_id: str) -> None:
    if not _VIDEO_ID_RE.match(video_id or ""):
        raise HTTPException(422, "video_id must be 6-16 [A-Za-z0-9_-]")
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    if host not in _ALLOWED_HOSTS:
        raise HTTPException(422, "only youtube.com / youtu.be URLs are accepted")


@router.get("/status")
async def status(request: Request):
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    return {"busy": _lock.locked()}


@router.post("/download")
async def download(request: Request):
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    if _lock.locked():
        raise HTTPException(429, "another download is already running; retry shortly")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "json body required") from None
    url = str(body.get("url") or "")
    video_id = str(body.get("video_id") or "")
    _validate(url, video_id)

    async with _lock:
        # yt_dlp imports lazily inside the worker machinery (~80 MB RSS);
        # importing inside the lock keeps accounting simple.
        from ..youtube_proxy import download_full_with_proxy

        base = Path(os.getenv("TEMP_DIR") or tempfile.gettempdir())
        dest = Path(tempfile.mkdtemp(prefix="relay-", dir=str(base)))
        try:
            path = await asyncio.wait_for(
                asyncio.to_thread(download_full_with_proxy, url, video_id, dest),
                timeout=1500,
            )
            if not path.exists() or path.stat().st_size == 0:
                raise HTTPException(502, "download produced no file")
            logger.info("fetch-relay: serving %s (%d bytes)",
                        path.name, path.stat().st_size)
            return FileResponse(
                str(path), media_type="video/mp4",
                filename=f"{video_id}.mp4",
                background=BackgroundTask(shutil.rmtree, str(dest), True),
            )
        except HTTPException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        except BaseException as exc:  # noqa: BLE001 - relay reports everything
            shutil.rmtree(dest, ignore_errors=True)
            logger.exception("fetch-relay download failed")
            raise HTTPException(
                502, f"download failed: {type(exc).__name__}: {exc}"[:400]
            ) from exc
