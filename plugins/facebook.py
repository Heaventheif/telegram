# -*- coding: utf-8 -*-
"""
plugins/facebook.py — فيسبوك
المزود الأول: facebook-video-download-api — نفحص وجود الصوت مباشرة على
رابط كل مرشّح عبر ffprobe (Range requests على الرؤوس فقط، بدون تنزيل
الملف كاملاً) قبل التنزيل، فننزّل المرشّح الصحيح مرة واحدة فقط بدل تجربة
كل مرشّح بتنزيله كاملاً للتأكد من الصوت.
المزود الثاني (احتياطي عند فشل الأول كلياً): yt-dlp مباشرة.
"""
import os, asyncio, shutil, tempfile, logging
import aiohttp
from config import config
from plugin_loader import ProbeResult, DownloadResult, QualityOption, get_http_session, stream_to_file, has_audio_stream

logger = logging.getLogger("plugin.facebook")

DESCRIPTION = "فيسبوك/ريلز — facebook-video-download-api (فحص صوت عن بُعد) + yt-dlp كخيار احتياطي"
DOMAINS     = ["facebook.com", "fb.watch", "fb.com"]
PRIORITY    = 10

UPLOAD_LIMIT = config.UPLOAD_LIMIT

_FDOWN = config.FB_DOWNLOAD_API
_UA    = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"

# مهلة عملية ffprobe الواحدة، ومهلة توقف الشبكة الداخلية لها (مايكروثانية)
_PROBE_TIMEOUT   = 15
_NET_STALL_US    = "10000000"  # 10 ثوانٍ

_OPTIONS = [
    QualityOption(kind="video", label="🎥 جودة عادية", key="v_sd",  size_hint=0),
    QualityOption(kind="video", label="🎥 جودة HD",    key="v_hd",  size_hint=0),
    QualityOption(kind="audio", label="🎵 256kbps",   key="a_256", size_hint=0),
    QualityOption(kind="audio", label="🎵 128kbps",   key="a_128", size_hint=0),
]

async def probe(url: str) -> ProbeResult:
    return ProbeResult(title="فيديو فيسبوك", options=_OPTIONS, extra={"url": url})


# ══════════════════════════════════════════════
# 🔇 فحص وجود مسار صوت على رابط بعيد مباشرة — بدون تنزيل الملف كاملاً.
# ffprobe يقرأ فقط الرؤوس اللازمة عبر Range requests، أسرع بكثير من تنزيل
# كل مرشّح للتأكد منه (كما كان يحدث سابقاً بعد التنزيل الكامل).
# ══════════════════════════════════════════════
async def _has_audio_stream_url(url: str) -> bool:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            "-timeout", _NET_STALL_US,
            "-user_agent", _UA,
            url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT)
        if proc.returncode != 0:
            return True  # فشل الفحص نفسه (رابط غير متاح مؤقتاً...) — لا نمنع الإرسال بسببه
        return bool(out.strip())
    except Exception:
        # فشل ffprobe نفسه (مهلة/خطأ شبكة عابر) ≠ فشل الفيديو — لا نمنع
        # الإرسال بسببه، نتساهل مثل has_audio_stream على الملفات المحلية
        if proc:
            try: proc.kill()
            except Exception: pass
        return True


async def _pick_url_for_quality(sess, url: str, quality: str):
    """يجرّب مرشّحي جودة معيّنة ويرجع أول رابط فيه صوت فعلي (فحص عن بُعد)،
    أو أول رابط نجح جلبه كاحتياط صامت إن لم يوجد أي مرشّح فيه صوت."""
    try:
        urls, title = await _get_candidates(sess, url, quality)
    except Exception as e:
        logger.warning(f"[fb-api] فشل جلب روابط للجودة {quality}: {e}")
        return None

    fallback = None
    for cand in urls:
        has_audio = await _has_audio_stream_url(cand)
        if has_audio:
            return {"url": cand, "title": title, "has_audio": True}
        if fallback is None:
            fallback = {"url": cand, "title": title, "has_audio": False}
    return fallback


async def download(url: str, choice: dict) -> DownloadResult:
    key = choice["key"]

    if key.startswith("a_"):
        return await _download_audio_via_ytdlp(url, key)

    quality = "720p" if key == "v_hd" else "worst"
    LADDER  = ["worst", "360p", "720p", "1080p", "best"]
    start   = LADDER.index(quality) if quality in LADDER else 0

    sess = await get_http_session()

    # نصعد سلّم الجودة ونتوقف فوراً عند أول مرشّح فيه صوت فعلي — بدل تجربة
    # كل السلّم دائماً. نحتفظ بأول مرشّح صامت كاحتياط إن لم نجد أي صوت.
    picked = silent_fallback = None
    for q in LADDER[start:]:
        result = await _pick_url_for_quality(sess, url, q)
        if not result:
            continue
        if result["has_audio"]:
            picked = result
            break
        if silent_fallback is None:
            silent_fallback = result
    if not picked:
        picked = silent_fallback

    if picked:
        try:
            fpath = await _stream_file(sess, picked["url"])
            return DownloadResult(file_path=fpath, title=picked["title"], is_audio=False)
        except Exception as e:
            logger.warning(f"[fb-api] فشل تنزيل الرابط المختار: {e}")

    # ── المزود الأول فشل تماماً → التبديل إلى yt-dlp كخيار ثانٍ ──
    logger.warning("[facebook] المزود الأول (API) فشل بالكامل — تجربة yt-dlp كخيار احتياطي")
    try:
        return await _download_via_ytdlp(url, key)
    except Exception as e2:
        logger.error(f"[facebook] فشل المزود الاحتياطي yt-dlp أيضاً: {e2}")
        raise Exception(f"فشل المزود الأساسي (API) والمزود الاحتياطي (yt-dlp): {e2}")


async def _download_via_ytdlp(url: str, key: str) -> DownloadResult:
    """مزود ثانٍ احتياطي: تحميل مباشر عبر yt-dlp عند فشل الـ API الخارجي."""
    import yt_dlp
    is_hd  = key == "v_hd"
    tmpdir = tempfile.mkdtemp(prefix="fb_ytdlp_")
    opts = {
        "http_headers": {"User-Agent": _UA},
        "retries": 5, "fragment_retries": 5,
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        # 🔇 نضمن دائماً مساراً صوتياً: أولاً دمج فيديو+صوت منفصلَين، ثم صيغة
        # جاهزة تحتوي صوتاً صراحة (acodec!=none)، وفقط كحل أخير أي شيء متاح
        "format": (
            "bestvideo[height<=720]+bestaudio/best[height<=720][acodec!=none]/best[acodec!=none]/best"
            if is_hd else
            "worstvideo[ext=mp4]+worstaudio/worst[ext=mp4][acodec!=none]/worst[acodec!=none]/worst"
        ),
        "merge_output_format": "mp4",
    }

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=180)
        title = (info or {}).get("title", "فيديو فيسبوك")

        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if not files:
            raise Exception("yt-dlp لم يُنتج أي ملف")
        final = max(files, key=os.path.getsize)

        fd, dest = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        os.remove(dest)             # ⚡ shutil.move بدل copy2 — نقل بدل نسخ مضاعف للملف
        shutil.move(final, dest)

        # ✅ تحقق أخير بعد اكتمال التنزيل/الدمج بالكامل — نرفض بوضوح بدل
        # إرسال فيديو صامت للمستخدم إن فشل كل شيء في تأمين مسار صوت
        if not await has_audio_stream(dest):
            os.remove(dest)
            raise Exception("الفيديو الناتج بدون صوت رغم كل محاولات الدمج")

        return DownloadResult(file_path=dest, title=title, is_audio=False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def _download_audio_via_ytdlp(url: str, key: str) -> DownloadResult:
    """استخراج صوت فقط من فيديو فيسبوك عبر yt-dlp (لا يوجد مزود خارجي للصوت).

    ⚠️ لا نستخدم postprocessor الداخلي FFmpegExtractAudio لأنه كان يفشل أحياناً
    بخطأ 'unable to obtain file audio codec with ffprobe' — يحدث عندما يختار
    yt-dlp أفضل مسار متاح (bestaudio/best) لكنه ليس مساراً صوتياً خالصاً
    يستطيع ffprobe التعرف عليه فوراً كصوت. بدلاً من ذلك: نُنزّل الملف الخام
    (فيديو أو صوت) بالكامل أولاً عبر yt-dlp حتى اكتماله التام، ثم نستخرج
    الصوت بأنفسنا عبر أمر ffmpeg مباشر (-vn) — أكثر موثوقية لأنه يعمل سواء
    كان الملف صوتاً خالصاً أو فيديو ممزوجاً بصوت."""
    import yt_dlp
    quality = key.split("_", 1)[1]   # "256" أو "128"
    tmpdir  = tempfile.mkdtemp(prefix="fb_audio_")
    opts = {
        "http_headers": {"User-Agent": _UA},
        "retries": 5, "fragment_retries": 5,
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "format": "bestaudio/best",
    }

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=180)
        title = (info or {}).get("title", "صوت فيسبوك")

        # ✅ نصل هنا فقط بعد اكتمال تنزيل yt-dlp للملف الخام بالكامل بنجاح
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if not files:
            raise Exception("yt-dlp لم يُنتج أي ملف صوتي")
        raw_path = max(files, key=os.path.getsize)

        fd, dest = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", raw_path, "-vn",
            "-acodec", "libmp3lame", "-b:a", f"{quality}k", dest,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
            if os.path.exists(dest):
                os.remove(dest)
            raise Exception(f"فشل استخراج الصوت عبر ffmpeg: {stderr.decode(errors='ignore')[:300]}")

        return DownloadResult(file_path=dest, title=title, is_audio=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _get_candidates(sess, url, quality):
    async with sess.post(
        f"{_FDOWN}/download",
        json={"url": url, "quality": quality},
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=20)
    ) as r:
        data = await r.json(content_type=None)
    if not data or data.get("status") == "error":
        raise Exception((data or {}).get("error") or "فشل جلب روابط فيسبوك")
    urls = []
    if data.get("download_url"): urls.append(data["download_url"])
    for fmt in (data.get("available_formats") or []):
        u = fmt.get("url") if isinstance(fmt, dict) else None
        if u and u not in urls: urls.append(u)
    title = (data.get("video_info") or {}).get("title") or "فيديو فيسبوك"
    return urls, title

async def _stream_file(sess, url) -> str:
    # ⚡ streaming غير-blocking عبر aiofiles + إيقاف مبكر إذا تجاوز حد الرفع
    return await stream_to_file(sess, url, ".mp4", timeout_total=120, max_size=UPLOAD_LIMIT)
