# -*- coding: utf-8 -*-
"""
plugins/ytdlp_generic.py
========================
Fallback عام لكل المواقع التي لا تحظر Render's IP مباشرة عند استخدام
yt-dlp (بعكس YouTube/Facebook/SoundCloud التي لها plugins مستقلة تعتمد
APIs خارجية لتفادي هذا الحظر تحديداً):
تيك توك، تويتر/X، انستغرام، Bilibili، Twitch، Reddit، وأي موقع آخر.
PRIORITY عالي (آخر من يُجرَّب) لأن yt-dlp عرضة لحظر IP على Render.

يستخدم aria2c كمُنزِّل خارجي (external_downloader) لكل المواقع — تحميل
متوازٍ للـ segments أسرع بكثير من مُنزِّل yt-dlp الداخلي، خصوصاً على
اتصالات بطيئة نسبياً مثل استضافات Render المجانية.

بعض المواقع تحتاج ترويسات HTTP خاصة (مثل Referer لـ Bilibili) — تُضاف
تلقائياً حسب نطاق الرابط عبر _headers_for() بدل ملف منفصل لكل موقع.

روابط قوائم التشغيل تُعامَل كعنصرها الأول فقط (noplaylist=True) — لا دعم ZIP.
"""
import os, asyncio, shutil, tempfile, logging
from urllib.parse import urlparse
from plugin_loader import ProbeResult, DownloadResult, QualityOption, has_audio_stream

logger = logging.getLogger("plugin.ytdlp_generic")

DESCRIPTION = "عام (yt-dlp + aria2c) — تيك توك، تويتر، انستغرام، Bilibili، Twitch، Reddit وغيرها"
DOMAINS     = ["*"]   # يقبل أي رابط لم يُطالب به plugin آخر
PRIORITY    = 99      # آخر خيار دائماً

# ← يضاف تلقائياً لـ Dockerfile عند حفظ هذا الملف في plugins/
DOCKERFILE_APT = ["aria2"]

_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"

STANDARD_HEIGHTS  = [1080, 720, 480, 360, 240, 144]
STANDARD_BITRATES = [256, 192, 128, 96, 64]

# ── ترويسات خاصة بمواقع محدَّدة (يحتاجها الموقع ليقبل الطلب أصلاً) ──
_SITE_EXTRA_HEADERS = {
    "bilibili.com": {"Referer": "https://www.bilibili.com/"},
    "b23.tv":       {"Referer": "https://www.bilibili.com/"},
}

def _headers_for(url: str) -> dict:
    host = urlparse(url).netloc.lower()
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    for domain, extra in _SITE_EXTRA_HEADERS.items():
        if domain in host:
            headers.update(extra)
            break
    return headers


def _base_opts(url: str) -> dict:
    return {
        "http_headers": _headers_for(url),
        "external_downloader": "aria2c",
        "external_downloader_args": ["--min-split-size=1M", "--max-connection-per-server=4"],
        "retries": 5, "fragment_retries": 5,
        "quiet": True, "no_warnings": True, "noplaylist": True,
    }

def _extract(yt_dlp, opts, url, dl):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=dl)


async def probe(url: str) -> ProbeResult:
    import yt_dlp
    loop = asyncio.get_running_loop()

    opts = _base_opts(url)
    info = await asyncio.wait_for(
        loop.run_in_executor(None, lambda: _extract(yt_dlp, opts, url, False)),
        timeout=45,
    )

    formats  = info.get("formats") or []
    duration = info.get("duration") or 0
    title    = info.get("title", "media")

    vfmts = [f for f in formats if f.get("vcodec") not in (None,"none") and f.get("height")]
    afmts = [f for f in formats if f.get("acodec") not in (None,"none") and f.get("vcodec") in (None,"none")]

    best_asize = max((f.get("filesize") or f.get("filesize_approx") or 0) for f in afmts) if afmts else 0
    if not best_asize and duration:
        best_asize = int(duration * 128 * 125)

    options = []
    real_h  = sorted({f["height"] for f in vfmts}, reverse=True)

    for h in STANDARD_HEIGHTS:
        if h not in real_h: continue
        cands = [f for f in vfmts if f["height"] == h]
        vsize = max((f.get("filesize") or f.get("filesize_approx") or 0) for f in cands) if cands else 0
        options.append(QualityOption(
            kind="video", label=f"🎥 {h}p", key=f"v_{h}",
            size_hint=(vsize or 0) + best_asize
        ))

    if not options and real_h:
        for rh in real_h[:4]:
            cands = [f for f in vfmts if f["height"] == rh]
            vsize = max((f.get("filesize") or f.get("filesize_approx") or 0) for f in cands) if cands else 0
            options.append(QualityOption(kind="video", label=f"🎥 {rh}p", key=f"v_{rh}", size_hint=(vsize or 0) + best_asize))

    max_abr = max((f.get("abr") or 0) for f in afmts) if afmts else 0
    for b in STANDARD_BITRATES:
        if max_abr and b > max_abr + 16: continue
        options.append(QualityOption(
            kind="audio", label=f"🎵 {b}kbps", key=f"a_{b}",
            size_hint=int(duration * b * 125) if duration else 0
        ))

    if not options:
        options = [QualityOption(kind="video", label="🎥 أفضل جودة", key="v_best", size_hint=0)]

    return ProbeResult(title=title, options=options[:12], extra={"url": url})


async def download(url: str, choice: dict) -> DownloadResult:
    key      = choice["key"]
    is_audio = key.startswith("a_")
    val      = key.split("_", 1)[1]

    import yt_dlp
    tmpdir = tempfile.mkdtemp(prefix="ytdlp_")
    opts   = _base_opts(url)
    opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")

    if is_audio:
        opts["format"] = f"bestaudio[abr<={val}]/bestaudio/best" if val != "best" else "bestaudio/best"
        # ⚠️ لا نستخدم postprocessor الداخلي FFmpegExtractAudio (كان يفشل أحياناً
        # بخطأ 'unable to obtain file audio codec with ffprobe' عند مواقع لا
        # تقدّم مساراً صوتياً خالصاً يستطيع ffprobe التعرّف عليه فوراً) — بدلاً
        # من ذلك نُنزّل الملف الخام كاملاً ثم نستخرج الصوت بأنفسنا عبر ffmpeg
        # مباشرة بعد اكتمال التنزيل (انظر التحويل أسفل الدالة).
    else:
        # 🔇 نضيف fallback يشترط acodec!=none صراحة قبل "best" النهائي —
        # لتفادي القبول بصيغة فيديو-فقط صامتة إن فشل دمج bestvideo+bestaudio
        opts["format"] = (
            f"bestvideo[height<={val}]+bestaudio/best[height<={val}][acodec!=none]/best[acodec!=none]/best"
            if val != "best" else "bestvideo+bestaudio/best[acodec!=none]/best"
        )
        opts["merge_output_format"] = "mp4"

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _extract(yt_dlp, opts, url, True)),
            timeout=180,
        )
        title = (info or {}).get("title", "media")

        # ✅ نصل هنا فقط بعد اكتمال تنزيل yt-dlp للملف الخام بالكامل بنجاح
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if not files: raise Exception("لم يُنتج أي ملف")
        final = max(files, key=os.path.getsize)

        if is_audio:
            # نستخرج الصوت بأنفسنا عبر ffmpeg بدل الاعتماد على postprocessor
            # هش داخل yt-dlp — يعمل سواء كان الملف الخام صوتاً خالصاً أو
            # فيديو ممزوجاً بصوت.
            bitrate = val if (val != "best" and val.isdigit()) else "192"
            fd, dest = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", final, "-vn",
                "-acodec", "libmp3lame", "-b:a", f"{bitrate}k", dest,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
                if os.path.exists(dest):
                    os.remove(dest)
                raise Exception(f"فشل استخراج الصوت عبر ffmpeg: {stderr.decode(errors='ignore')[:300]}")
            return DownloadResult(file_path=dest, title=title, is_audio=True)

        suffix = ".mp4"
        fd, dest = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        os.remove(dest)          # ⚡ shutil.move بدل copy2 — نقل بدل نسخ مضاعف للملف
        shutil.move(final, dest)

        # ✅ تحقق أخير بعد اكتمال التنزيل/الدمج بالكامل — نرفض بوضوح بدل
        # إرسال فيديو صامت للمستخدم
        if not await has_audio_stream(dest):
            os.remove(dest)
            raise Exception("الفيديو الناتج بدون صوت رغم كل محاولات الدمج")

        return DownloadResult(file_path=dest, title=title, is_audio=is_audio)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
