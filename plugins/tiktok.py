# -*- coding: utf-8 -*-
"""
plugins/tiktok.py
تيك توك — Bun worker (TikWM ثم TikMate كمزود احتياطي، مع retry/backoff)
بدل yt-dlp الذي بات يتعطل كثيراً على روابط تيك توك (video not available).
PRIORITY=10 أقل من ytdlp_generic (99) فيُختار هذا البلجن أولاً لروابط
tiktok.com، ولا يصل الطلب لـ ytdlp_generic إلا لو هذا البلجن غير موجود.
"""
import asyncio
import json
import logging
import os
import tempfile

from config import config
from plugin_loader import ProbeResult, DownloadResult, QualityOption, get_http_session, stream_to_file

logger = logging.getLogger("plugin.tiktok")

DESCRIPTION = "تيك توك — Bun/TikWM+TikMate (بديل عن yt-dlp)"
DOMAINS     = ["tiktok.com"]
PRIORITY    = 10

UPLOAD_LIMIT = config.UPLOAD_LIMIT

_JS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_js")
_JS_WORKER = os.path.join(_JS_DIR, "tiktok_worker.js")
_BUN_BIN   = os.getenv("BUN_BIN", "bun")

# لا تبعيات npm لهذا الـ worker (fetch/AbortController/Bun.sleep مدمجة في
# Bun) — يحتاج فقط ثنائي Bun نفسه، لذا نصرّح بتثبيته هنا أيضاً (بشكل
# مستقل عن plugins/youtube.py) لضمان عمل هذا البلجن وحده لو أُزيل الآخر.
DOCKERFILE_APT = ["curl", "unzip"]
DOCKERFILE_RUN = [
    "curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash",
]

_OPTIONS = [
    QualityOption(kind="video", label="🎥 فيديو",     key="v_hd",  size_hint=0),
    QualityOption(kind="audio", label="🎵 256kbps",   key="a_256", size_hint=0),
    QualityOption(kind="audio", label="🎵 128kbps",   key="a_128", size_hint=0),
]


async def _resolve(url: str) -> tuple:
    """يشغّل tiktok_worker.js عبر Bun ويرجع (رابط_فيديو_مباشر, عنوان)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _BUN_BIN, "run", _JS_WORKER, "resolve", url,
            cwd=_JS_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=25)
    except FileNotFoundError:
        raise Exception("tiktok(bun): الأمر bun غير مثبَّت — راجع DOCKERFILE_RUN")
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        raise Exception("tiktok(bun): انتهت المهلة")

    lines = [l for l in out_b.decode(errors="ignore").splitlines() if l.strip()]
    if not lines:
        raise Exception(f"tiktok(bun): لا يوجد إخراج | stderr={err_b.decode(errors='ignore')[:200]}")
    try:
        data = json.loads(lines[-1])
    except Exception:
        raise Exception(f"tiktok(bun): استجابة غير صالحة | {lines[-1][:200]}")

    if not data.get("status") or not data.get("url"):
        raise Exception(data.get("error", "tiktok(bun): فشل حل الرابط"))
    return data["url"], data.get("title", "فيديو تيك توك")


async def probe(url: str) -> ProbeResult:
    title = "فيديو تيك توك"
    try:
        _, title = await _resolve(url)
    except Exception as e:
        logger.warning(f"[tiktok][probe] فشل: {e}")
    return ProbeResult(title=title, options=_OPTIONS, extra={"url": url})


async def download(url: str, choice: dict) -> DownloadResult:
    key  = choice["key"]
    sess = await get_http_session()

    dl_url, title = await _resolve(url)
    video_path = await stream_to_file(sess, dl_url, ".mp4", timeout_total=60, max_size=UPLOAD_LIMIT)

    if key.startswith("a_"):
        quality = key.split("_", 1)[1]
        try:
            audio_path = await _extract_audio(video_path, quality)
        finally:
            try: os.remove(video_path)
            except Exception: pass
        return DownloadResult(file_path=audio_path, title=title, is_audio=True)

    return DownloadResult(file_path=video_path, title=title, is_audio=False)


async def _extract_audio(video_path: str, quality: str) -> str:
    fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix="tiktok_audio_")
    os.close(fd)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", video_path, "-vn",
        "-acodec", "libmp3lame", "-b:a", f"{quality}k", out_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise Exception(f"فشل استخراج الصوت عبر ffmpeg: {stderr.decode(errors='ignore')[:300]}")
    return out_path
