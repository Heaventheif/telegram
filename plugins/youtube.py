# -*- coding: utf-8 -*-
"""
plugins/youtube.py
يوتيوب: 3 مزودين بالترتيب — Bun/@vreden/youtube_scraper (أول خيار) ثم
ccproject ثم yt-dlp-stream.

منطق حل رابط التحميل عبر @vreden/youtube_scraper موجود في ملف JS حقيقي
(plugins/_js/youtube_worker.js) يُشغَّل عبر Bun كـ subprocess؛ هذا الملف
يبقى الواجهة البايثونية المطلوبة من plugin_loader (import + probe/download
كـ coroutines). فحوصات has_video_stream/is_black_video تبقى بايثون.
"""
import asyncio
import json
import logging
import os
import shutil

import aiohttp
from config import config
from plugin_loader import ProbeResult, DownloadResult, QualityOption, get_http_session, stream_to_file, has_video_stream, is_black_video

logger = logging.getLogger("plugin.youtube")

DESCRIPTION = "يوتيوب — Bun/@vreden/youtube_scraper (أول خيار) + ccproject + yt-dlp-stream"
DOMAINS     = ["youtube.com", "youtu.be"]
PRIORITY    = 10

# نفس حد الرفع المركزي في config.py — يُستخدم لإيقاف التحميل مبكراً إذا تجاوزه الملف
UPLOAD_LIMIT = config.UPLOAD_LIMIT

_UA        = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"
_CCPROJECT = config.YT_API_1
_YT2_BASE  = config.YT_API_2

# Bun worker — plugins/_js/youtube_worker.js
_JS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_js")
_JS_WORKER = os.path.join(_JS_DIR, "youtube_worker.js")
_BUN_BIN   = shutil.which(os.getenv("BUN_BIN", "bun")) or os.getenv("BUN_BIN", "bun")

DOCKERFILE_APT = ["curl", "unzip"]
DOCKERFILE_RUN = [
    "curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash",
]


async def setup():
    """تثبيت تبعيات npm لـ youtube_worker.js عبر bun install — تُنفَّذ عند
    الإقلاع لأن plugins/_js/ غير متاح داخل الصورة إلا بعد COPY . ."""
    node_modules = os.path.join(_JS_DIR, "node_modules")
    if os.path.isdir(node_modules):
        logger.info("[youtube][bun] ✅ node_modules موجودة مسبقاً — تخطي bun install")
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            _BUN_BIN, "install",
            cwd=_JS_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.warning(f"[youtube][bun] ⚠️ فشل bun install: {err.decode(errors='ignore')[:300]}")
        else:
            logger.info("[youtube][bun] ✅ تم تثبيت تبعيات @vreden/youtube_scraper")
    except FileNotFoundError:
        logger.warning("[youtube][bun] ⚠️ الأمر bun غير موجود على PATH — تحقق من DOCKERFILE_RUN/BUN_BIN")
    except asyncio.TimeoutError:
        logger.warning("[youtube][bun] ⚠️ bun install تجاوز المهلة (60ث)")
    except Exception as e:
        logger.warning(f"[youtube][bun] ⚠️ خطأ أثناء bun install: {e}")

# جودات ثابتة — لا نستخدم yt-dlp للفحص حتى نتجنب حظر IP
_VIDEO_OPTIONS = [
    QualityOption(kind="video", label="🎥 1080p", key="v_1080", size_hint=0),
    QualityOption(kind="video", label="🎥 720p",  key="v_720",  size_hint=0),
    QualityOption(kind="video", label="🎥 480p",  key="v_480",  size_hint=0),
    QualityOption(kind="video", label="🎥 360p",  key="v_360",  size_hint=0),
]
_AUDIO_OPTIONS = [
    QualityOption(kind="audio", label="🎵 256kbps", key="a_256", size_hint=0),
    QualityOption(kind="audio", label="🎵 128kbps", key="a_128", size_hint=0),
    QualityOption(kind="audio", label="🎵 64kbps",  key="a_64",  size_hint=0),
]


async def _run_bun_worker(*args: str, timeout: int = 30) -> dict:
    """يشغّل youtube_worker.js عبر Bun ويرجع آخر سطر JSON من stdout كـ dict
    (نأخذ آخر سطر لأن الحزمة قد تطبع ضجيج تشخيصي إضافي قبل نتيجتنا)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _BUN_BIN, "run", _JS_WORKER, *args,
            cwd=_JS_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        raise Exception("vreden(bun): الأمر bun غير مثبَّت — راجع DOCKERFILE_RUN")
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        raise Exception("vreden(bun): انتهت المهلة")

    lines = [l for l in out_b.decode(errors="ignore").splitlines() if l.strip()]
    if not lines:
        raise Exception(f"vreden(bun): لا يوجد إخراج | stderr={err_b.decode(errors='ignore')[:200]}")
    try:
        data = json.loads(lines[-1])
    except Exception:
        raise Exception(f"vreden(bun): استجابة غير صالحة | {lines[-1][:200]}")

    if not data.get("status"):
        raise Exception(data.get("error", "vreden(bun): فشل غير معروف"))
    return data


async def probe(url: str) -> ProbeResult:
    """
    نجلب عنوان الفيديو أولاً عبر Bun/@vreden/youtube_scraper، وإن فشل
    نرجع لـ yt2 API — دون تحميل الجودات الكاملة، ثم نعرض قائمة ثابتة.
    (روابط قوائم التشغيل تُعامَل كالفيديو الأول فيها فقط — لا دعم ZIP.)
    """
    title = None

    try:
        data = await _run_bun_worker("probe", url, timeout=15)
        title = data.get("title")
    except Exception as e:
        logger.warning(f"[probe][vreden] فشل: {e}")

    if not title:
        try:
            from urllib.parse import quote
            sess = await get_http_session()
            async with sess.get(
                f"{_YT2_BASE}/v2/q?={quote(url)}",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data = await r.json(content_type=None)
            if isinstance(data, list): data = data[0] if data else {}
            title = (data or {}).get("title")
        except Exception as e:
            logger.warning(f"[probe][yt2] فشل جلب العنوان: {e}")

    return ProbeResult(
        title=title or "فيديو يوتيوب",
        options=_VIDEO_OPTIONS + _AUDIO_OPTIONS,
        extra={"url": url},
    )


async def download(url: str, choice: dict) -> DownloadResult:
    key = choice["key"]
    is_audio = key.startswith("a_")
    val      = key.split("_", 1)[1]   # "720", "128", ...

    sess   = await get_http_session()
    errors = []
    for provider in (_via_vreden, _via_ccproject, _via_yt2):
        try:
            dl_url, title = await provider(sess, url, not is_audio, val)
            suffix = ".mp3" if is_audio else ".mp4"
            fpath  = await _stream_file(sess, dl_url, suffix)
            # 🖤 بعض مزودي يوتيوب المجانيين يرجعون أحياناً ملف صوت فقط
            # مُسمّى .mp4 (يظهر عند المستخدم كصورة سوداء + صوت) — نتحقق
            # بعد اكتمال التنزيل ونرفض المرشح المزيّف لتجربة المزود التالي
            if not is_audio and not await has_video_stream(fpath):
                logger.warning(f"[download] المزود {provider.__name__} أعاد ملفاً بدون فيديو حقيقي — تجربة مزود آخر")
                try: os.remove(fpath)
                except Exception: pass
                errors.append(f"{provider.__name__}: الملف المُرجَع بدون مسار فيديو (صوت فقط)")
                continue

            # 🖤 بعض المزودين يضعون مساراً فيديو "حقيقياً" لكنه مجرد إطار
            # أسود ثابت مكرَّر طوال المدة — يجتاز فحص has_video_stream أعلاه
            # رغم أنه يظهر عند المستخدم كصورة سوداء + صوت أيضاً
            if not is_audio and await is_black_video(fpath):
                logger.warning(f"[download] المزود {provider.__name__} أعاد فيديو أسود بالكامل — تجربة مزود آخر")
                try: os.remove(fpath)
                except Exception: pass
                errors.append(f"{provider.__name__}: الفيديو المُرجَع صورة سوداء ثابتة")
                continue
            return DownloadResult(file_path=fpath, title=title, is_audio=is_audio)
        except Exception as e:
            logger.warning(f"[download] المزود {provider.__name__} فشل: {e}")
            errors.append(f"{provider.__name__}: {e}")
            continue

    logger.error(f"[download] فشل كل المزودين | url={url} | {errors}")
    raise Exception("فشل كل المزودين الخارجيين:\n" + "\n".join(errors))


async def _via_vreden(sess, url, want_mp4, val):
    """المزود الأول — يحل رابط التحميل عبر Bun worker (@vreden/youtube_scraper).
    `sess` غير مستخدَم؛ موجود فقط ليطابق توقيع بقية المزودين."""
    kind = "video" if want_mp4 else "audio"
    data = await _run_bun_worker("download", kind, str(val), url, timeout=30)
    dl_url = data.get("url")
    if not dl_url:
        raise Exception("vreden(bun): لا يوجد رابط تحميل في الاستجابة")
    return dl_url, data.get("title", "يوتيوب")


async def _via_ccproject(sess, url, want_mp4, val):
    kind = "mp4" if want_mp4 else "mp3"
    async with sess.get(
        _CCPROJECT, params={"url": url, "type": kind},
        timeout=aiohttp.ClientTimeout(total=30)
    ) as r:
        data = await r.json(content_type=None)
    if not isinstance(data, dict) or not data.get("download"):
        raise Exception(data.get("error") if isinstance(data, dict) else "no download URL")
    return data["download"], data.get("title", "يوتيوب")


async def _via_yt2(sess, url, want_mp4, val):
    from urllib.parse import quote
    async with sess.get(
        f"{_YT2_BASE}/v2/q?={quote(url)}",
        timeout=aiohttp.ClientTimeout(total=30)
    ) as r:
        data = await r.json(content_type=None)
    if isinstance(data, list): data = data[0] if data else {}
    media = (data or {}).get("media") or {}
    def _u(v): return v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else None)
    dl_url = _u(media.get("mp4") if want_mp4 else media.get("mp3"))
    if not dl_url:
        raise Exception("yt2: لا يوجد رابط تحميل")
    return dl_url, data.get("title", "يوتيوب")


async def _stream_file(sess, url, suffix) -> str:
    # ⚡ streaming غير-blocking عبر aiofiles + إيقاف مبكر إذا تجاوز حد الرفع
    return await stream_to_file(sess, url, suffix, timeout_total=120, max_size=UPLOAD_LIMIT)
