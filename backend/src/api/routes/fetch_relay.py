"""Agency fetch relay.

Lets the operator's GTA6 agency pipeline download a YouTube video through
this deployment's IP (the agency sandbox IP is bot-flagged by YouTube).

- POST /fetch-relay/download  {"url": ..., "video_id": ...}
  Header: x-relay-token: <FETCH_RELAY_TOKEN>
- Exactly ONE download runs at a time (512 MB free plan: the OOM rule).
- Plain yt-dlp (same binary the preflight uses, deno JS runtime, default
  player clients, height capped by YOUTUBE_MAX_HEIGHT). No cookies, no
  proxy, no third parties.
- The file streams back as video/mp4; staging dir is removed afterwards.
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

_MAX_HEIGHT = max(144, min(int(os.getenv("YOUTUBE_MAX_HEIGHT", "720") or 720), 1080))


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


def _direct_download(url: str, dest: Path, video_id: str) -> Path:
    """Straight yt-dlp download into dest. Raises with a readable message."""
    import yt_dlp

    opts = {
        "format": (f"bv*[height<={_MAX_HEIGHT}]+ba/"
                   f"b[height<={_MAX_HEIGHT}]/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(dest / f"{video_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if shutil.which("deno"):
        opts["js_runtimes"] = {"deno": {}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    for cand in sorted(dest.glob(f"{video_id}.*")):
        if cand.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            return cand
    raise RuntimeError("yt-dlp reported success but no media file found")


@router.get("/status")
async def status(request: Request):
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    return {"busy": _lock.locked(), "max_height": _MAX_HEIGHT}


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
        base = Path(os.getenv("TEMP_DIR") or tempfile.gettempdir())
        dest = Path(tempfile.mkdtemp(prefix="relay-", dir=str(base)))
        try:
            # yt_dlp imports lazily (~80 MB RSS on this 512 MB box)
            path = await asyncio.wait_for(
                asyncio.to_thread(_direct_download, url, dest, video_id),
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
