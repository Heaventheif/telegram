# -*- coding: utf-8 -*-
"""
plugin_loader.py
=====================================
يكتشف plugins/*.py تلقائياً ويولّد Dockerfile نهائياً من Dockerfile.base.

يدعم ثلاثة أنواع من الـ plugins:

── النوع الأول: URL-plugin (تحميل عبر رابط) ──
    DOMAINS  = ["youtube.com"]     # (إلزامي) روابط يدعمها
    PRIORITY = 10                  # (اختياري) أقل = أول — افتراضي 50
    DESCRIPTION = "وصف"           # (اختياري)

    async def probe(url: str) -> ProbeResult: ...
    async def download(url: str, choice: dict) -> DownloadResult: ...

── النوع الثاني: Handler-plugin (يستقبل رسائل مباشرة كصوت/فيديو) ──
    def register_plugin():
        # يُرجع dict واحد أو قائمة dicts بالشكل:
        #   {"filter": fn(msg: dict) -> bool, "callback": async fn(msg: dict, bot: Bot)}
        # fn(msg) تفحص قاموس رسالة تيليجرام الخام (راجع telegram_api.is_*)
        return {"filter": is_recognizable_media, "callback": my_callback}

── النوع الثالث: Search-plugin (بحث عن أغاني بالاسم عبر منصة) ──
    SEARCH_PRIORITY = 10   # (اختياري) ترتيب عرض نتائج هذا المزود — أقل = أول

    async def search(query: str) -> List[SearchResult]: ...
    # يُستدعى عندما يرسل المستخدم نصاً عادياً (وليس رابطاً)؛
    # النتائج المُرجعة (حتى 10 لكل مزود) تُعرض للمستخدم كأزرار،
    # واختيار أحدها يُمرَّر رابطه لمسار URL-plugin العادي تلقائياً.

كل الأنواع يمكنها الإعلان عن:
    # حزم apt إضافية يحتاجها الـ plugin:
    DOCKERFILE_APT = ["libsndfile1", "libmagic1"]

    # أوامر RUN shell إضافية (مثل playwright install):
    DOCKERFILE_RUN = ["playwright install chromium", "playwright install-deps chromium"]

    # حزم pip إضافية غير موجودة في requirements.txt:
    DOCKERFILE_PIP = ["some-package==1.2.3"]

    async def setup(): ...   # (اختياري) يُنفَّذ مرة واحدة عند الإقلاع
"""

import os, sys, glob, importlib, importlib.util, logging, hashlib, time, tempfile, asyncio, re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("plugin_loader")

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
PLUGINS_DIR     = os.path.join(BASE_DIR, "plugins")
DOCKERFILE_BASE = os.path.join(BASE_DIR, "Dockerfile.base")
DOCKERFILE_OUT  = os.path.join(BASE_DIR, "Dockerfile")

# ══════════════════════════════════════════════
# هياكل البيانات المشتركة
# ══════════════════════════════════════════════

@dataclass
class QualityOption:
    kind:      str    # "video" | "audio"
    label:     str    # نص الزر
    key:       str    # معرف داخلي
    size_hint: int = 0

@dataclass
class ProbeResult:
    title:   str
    options: List[QualityOption]
    extra:   dict = field(default_factory=dict)

@dataclass
class DownloadResult:
    file_path:   str
    title:       str
    is_audio:    bool
    is_document: bool = False   # True لإرسال الملف كمستند (مثل ZIP) بدل فيديو/صوت
    extra:       dict = field(default_factory=dict)

@dataclass
class SearchResult:
    title:    str
    url:      str
    source:   str          # اسم المنصة: "YouTube", "SoundCloud"...
    duration: str = ""     # نص مختصر مثل "3:45"
    uploader: str = ""

# ══════════════════════════════════════════════
# سجل الـ plugins
# ══════════════════════════════════════════════

_plugins:          list = []
_registry:         dict = {}
_extra_handlers:   list = []   # Handlers مباشرة من handler-plugins (مثل Shazam)
_search_providers: list = []   # مزودو البحث النصي (مثل search_youtube, search_soundcloud)
_pending_setups:   list = []   # [(name, setup_coro_fn), ...] تُنتظر عبر run_pending_setups()

def get_registry()         -> dict: return _registry
def get_plugins()          -> list: return _plugins
def get_extra_handlers()   -> list: return _extra_handlers
def get_search_providers() -> list: return _search_providers

# ══════════════════════════════════════════════
# 🌐 جلسة aiohttp مشتركة — لتسريع المعالجة والإرسال
# (تجنّب فتح اتصال TCP/TLS جديد مع كل تحميل، وإعادة استخدام
#  اتصالات keep-alive بين الطلبات المتتالية والمتزامنة)
# ══════════════════════════════════════════════

_http_session = None

async def get_http_session():
    """يرجع جلسة aiohttp مشتركة (يُنشئها عند أول استخدام)."""
    global _http_session
    if _http_session is None or _http_session.closed:
        import aiohttp
        connector = aiohttp.TCPConnector(
            limit=100,          # أقصى عدد اتصالات مفتوحة إجمالاً
            limit_per_host=20,  # أقصى اتصالات متزامنة لكل مضيف
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=180, connect=20)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        logger.info("[http] ✅ تم إنشاء جلسة aiohttp مشتركة")
    return _http_session

async def close_http_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("[http] 🔒 تم إغلاق الجلسة المشتركة")

# ══════════════════════════════════════════════
# 🚦 حد أقصى للتحميلات المتزامنة (يحمي الرام والقرص من
#    ذروة استهلاك عند طلبات متعددة بنفس الوقت — خصوصاً على
#    استضافات بموارد محدودة مثل Render Free)
# ══════════════════════════════════════════════

_download_semaphore = None

def get_download_semaphore():
    """يرجع Semaphore مشتركة تحدّ عدد عمليات download() المتزامنة.
    القيمة تُضبط عبر MAX_CONCURRENT_DOWNLOADS (راجع config.py — مصدر
    الحقيقة الوحيد لكل متغيرات البيئة، افتراضياً 2)."""
    global _download_semaphore
    if _download_semaphore is None:
        import asyncio
        from config import config
        limit = config.MAX_CONCURRENT_DOWNLOADS
        _download_semaphore = asyncio.Semaphore(limit)
        logger.info(f"[download] 🚦 الحد الأقصى للتحميلات المتزامنة: {limit}")
    return _download_semaphore

# ══════════════════════════════════════════════
# ⬇️ تنزيل بالتدفّق (streaming) إلى ملف مؤقت — بدون تحميل
#    المحتوى بالذاكرة، مع إيقاف مبكر إذا تجاوز الحجم المسموح
#    (يمنع هدر الوقت/الباندويدث/القرص على ملفات كبيرة سترفض لاحقاً)
# ══════════════════════════════════════════════

class _SizeExceeded(Exception):
    """تجاوز max_size أثناء التدفّق — قرار حتمي، لا نعيد المحاولة عنده."""


async def stream_to_file(sess, url: str, suffix: str, *, headers: dict = None,
                          timeout_total: int = 120, max_size: int = None,
                          chunk_size: int = 262144, retries: int = 2) -> str:
    """يحمّل url إلى ملف مؤقت عبر aiofiles (كتابة غير-blocking للـ event loop).
    لا يُرجع المسار أبداً إلا بعد اكتمال التدفّق بالكامل بنجاح — أي استدعاء
    لاحق للقص/التقسيم يعمل دائماً على ملف كامل 100%، وليس على تنزيل جزئي.

    عند انقطاع الشبكة العابر منتصف التدفّق (مثل ContentLengthError أو قطع
    الاتصال من المزود الخارجي) نُعيد المحاولة تلقائياً حتى `retries` مرات
    إضافية قبل الاستسلام. يُحذف أي ملف جزئي بعد كل محاولة فاشلة ولا يُترك
    على القرص. تجاوز max_size وحده لا يُعاد تكراره لأنه قرار حتمي (نفس
    الحجم سيتكرر في كل محاولة)."""
    import aiohttp, aiofiles

    last_err = None
    for attempt in range(1, retries + 2):  # retries=2 → 3 محاولات إجمالاً
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        downloaded = 0
        try:
            async with sess.get(url, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=timeout_total)) as r:
                r.raise_for_status()
                async with aiofiles.open(path, "wb") as f:
                    async for chunk in r.content.iter_chunked(chunk_size):
                        downloaded += len(chunk)
                        if max_size and downloaded > max_size:
                            raise _SizeExceeded(
                                f"الملف يتجاوز الحجم المسموح ({max_size/1024/1024:.0f}MB) — تم إيقاف التحميل مبكراً"
                            )
                        await f.write(chunk)
            if downloaded == 0:
                raise Exception("الملف المُنزَّل فارغ")
            return path  # ✅ التنزيل اكتمل بالكامل — الآن فقط نُرجع المسار
        except _SizeExceeded:
            if os.path.exists(path):
                os.remove(path)
            raise
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            last_err = e
            if attempt <= retries:
                logger.warning(
                    f"[stream_to_file] محاولة {attempt}/{retries+1} فشلت "
                    f"({e}) — إعادة المحاولة من الصفر..."
                )
                await asyncio.sleep(1.5 * attempt)
                continue
            raise
    raise last_err  # لن نصل هنا عملياً


# ══════════════════════════════════════════════
# 🔇 التحقق من وجود مسار صوت في ملف مُنزَّل (فيديو صامت = بدون صوت)
#    بعض المزودين الخارجيين (خصوصاً لفيسبوك) قد يعيدون أحياناً رابط
#    فيديو-فقط بدون مسار صوت — نتحقق بعد التنزيل الكامل مباشرة قبل قبول
#    الملف، لنعطي فرصة للمزود التالي/الاحتياطي بدل إرسال فيديو صامت للمستخدم
# ══════════════════════════════════════════════

async def has_audio_stream(path: str) -> bool:
    """يرجع True إن كان الملف (بعد اكتمال تنزيله بالكامل) يحتوي على الأقل
    مسار صوت واحد يمكن لـ ffprobe التعرّف عليه، False خلاف ذلك (فيديو صامت)
    أو إذا تعذّر تشغيل ffprobe لأي سبب (نتساهل ونعتبره يحتوي صوتاً بدل رفض
    ملف سليم بسبب خطأ عابر في الفحص نفسه)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return True  # فشل الفحص نفسه — لا نرفض الملف بسببه
        return b"audio" in out
    except Exception:
        logger.warning(f"[has_audio_stream] تعذّر فحص الملف: {path}", exc_info=True)
        return True


# ══════════════════════════════════════════════
# 🖤 التحقق من وجود مسار فيديو حقيقي في ملف مُفترَض أنه فيديو
#    بعض المزودين الخارجيين (خصوصاً APIs يوتيوب المجانية) قد يعيدون أحياناً
#    رابط تحميل هو ملف صوت فقط لكنه مُسمّى .mp4 — تيليجرام يعرض هذا الملف
#    كصورة سوداء ثابتة مع مسار صوت. نتحقق بعد التنزيل الكامل مباشرة قبل
#    قبول الملف، لنعطي فرصة للمزود التالي/الاحتياطي بدل إرسال "فيديو" مزيّف
# ══════════════════════════════════════════════

async def has_video_stream(path: str) -> bool:
    """يرجع True إن كان الملف (بعد اكتمال تنزيله بالكامل) يحتوي على الأقل
    مسار فيديو واحد يمكن لـ ffprobe التعرّف عليه، False خلاف ذلك (الملف
    صوت فقط رغم امتداده) أو إذا تعذّر تشغيل ffprobe لأي سبب (نتساهل ونعتبره
    يحتوي فيديو بدل رفض ملف سليم بسبب خطأ عابر في الفحص نفسه)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return True  # فشل الفحص نفسه — لا نرفض الملف بسببه
        return b"video" in out
    except Exception:
        logger.warning(f"[has_video_stream] تعذّر فحص الملف: {path}", exc_info=True)
        return True


async def is_black_video(path: str, *, black_ratio_threshold: float = 0.98) -> bool:
    """يرجع True إذا كان الفيديو (رغم احتوائه فعلياً على مسار فيديو صالح)
    عبارة عن صورة سوداء ثابتة طوال مدته تقريباً — وهو ما تُرجعه بعض واجهات
    التحويل المجانية بدل رفض الطلب: تضع إطاراً أسود واحداً وتُكرره كمسار
    فيديو "حقيقي" حتى يجتاز فحص has_video_stream، بينما يظهر عند المستخدم
    كصورة سوداء مع صوت. نستخدم مرشّح ffmpeg blackdetect ونقارن مجموع مدة
    المقاطع السوداء بمدة الفيديو الكلية.
    يرجع False (نتساهل) إذا تعذّر الفحص أو مدة الفيديو غير معروفة، حتى لا
    نرفض ملفاً سليماً بسبب خطأ عابر في الفحص نفسه."""
    try:
        duration = await _ffprobe_duration(path)
        if not duration or duration <= 0:
            return False

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", path,
            "-vf", "blackdetect=d=0.1:pic_th=0.98",
            "-an", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        text = stderr.decode(errors="ignore")

        black_total = 0.0
        for m in re.finditer(r"black_duration:\s*([\d.]+)", text):
            black_total += float(m.group(1))

        return (black_total / duration) >= black_ratio_threshold
    except Exception:
        logger.warning(f"[is_black_video] تعذّر فحص الملف: {path}", exc_info=True)
        return False


# ══════════════════════════════════════════════
# ✂️ تقسيم ملف كبير (>50MB حد تيليجرام) إلى أجزاء قابلة للتشغيل
#    عبر ffmpeg -f segment -c copy (بدون إعادة ترميز — سريع لأن
#    الجودة المطلوبة محمَّلة مسبقاً، فقط يُعاد تغليفها بحاويات أصغر)
# ══════════════════════════════════════════════

async def _ffprobe_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0

async def split_media(path: str, *, max_size: int, is_audio: bool) -> List[str]:
    """يقسّم ملف صوت/فيديو أكبر من max_size إلى أجزاء عبر ffmpeg segment.
    يرجع قائمة مسارات الأجزاء (بترتيبها). يرمي استثناء عند الفشل."""
    fsize = os.path.getsize(path)
    if fsize <= max_size:
        return [path]

    duration = await _ffprobe_duration(path)
    if duration <= 0:
        raise Exception("تعذّر تحديد مدة الملف لتقسيمه")

    # نستهدف هامش أمان 90% من الحد لتفادي تجاوز الحجم بسبب فروقات الحاويات
    avg_bitrate_bps = (fsize * 8) / duration
    segment_seconds = max(5, int((max_size * 0.9 * 8) / avg_bitrate_bps))

    ext      = os.path.splitext(path)[1] or (".m4a" if is_audio else ".mp4")
    out_tmpl = tempfile.mkstemp(suffix="")[1]
    os.remove(out_tmpl)
    pattern  = out_tmpl + "_part_%03d" + ext

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", path,
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        pattern,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"فشل تقسيم الملف عبر ffmpeg: {stderr.decode()[:300]}")

    prefix = os.path.basename(out_tmpl) + "_part_"
    folder = os.path.dirname(out_tmpl) or "."
    parts  = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.startswith(prefix)
    )
    if not parts:
        raise Exception("ffmpeg لم يُنتج أي أجزاء")

    oversized = [p for p in parts if os.path.getsize(p) > max_size]
    if oversized:
        logger.warning(f"[split_media] {len(oversized)} جزء تجاوز الحد رغم التقسيم — سيُرسل كما هو")

    return parts

# ══════════════════════════════════════════════
# 🎵 SoundCloud client_id — كاش مشترك بين plugins/soundcloud.py
#    و plugins/search_soundcloud.py لتجنّب استخراج/طلب مكرّر
#    (نفس المنطق، مصدر واحد للحقيقة)
# ══════════════════════════════════════════════

import re as _re
_SC_SCRIPT_RE = _re.compile(r'https://a-v2\.sndcdn\.com/assets/[^"]+\.js')
_SC_CLIENT_RE = _re.compile(r'client_id:"([a-zA-Z0-9]{20,32})"')

_sc_client_id  = None
_sc_client_exp = 0.0

async def get_soundcloud_client_id(sess, ua_headers: dict) -> str:
    """يرجع client_id مشترك لـ SoundCloud API (كاش لمدة 12 ساعة)."""
    global _sc_client_id, _sc_client_exp
    import aiohttp
    if _sc_client_id and time.time() < _sc_client_exp:
        return _sc_client_id

    async with sess.get("https://soundcloud.com", headers=ua_headers,
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
        page = await r.text()

    for surl in _SC_SCRIPT_RE.findall(page)[-6:]:
        try:
            async with sess.get(surl, headers=ua_headers,
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                m = _SC_CLIENT_RE.search(await r.text())
            if m:
                _sc_client_id  = m.group(1)
                _sc_client_exp = time.time() + 12 * 3600
                return _sc_client_id
        except Exception:
            continue

    raise Exception("فشل استخراج client_id من SoundCloud")

def find_plugin(url: str):
    low = url.lower()
    for p in _plugins:
        for domain in (p.get("domains") or []):
            if domain != "*" and domain.lower() in low:
                return p
    for p in _plugins:
        if "*" in (p.get("domains") or []):
            return p
    return None

# ══════════════════════════════════════════════
# توليد Dockerfile ديناميكياً
# ══════════════════════════════════════════════

def _collect_dockerfile_deps(modules: list) -> dict:
    """يجمع DOCKERFILE_APT / DOCKERFILE_RUN / DOCKERFILE_PIP من كل الـ plugins."""
    apt_pkgs = []
    run_cmds = []
    pip_pkgs = []
    seen_apt = set()
    seen_run = set()
    seen_pip = set()

    for mod in modules:
        for pkg in (getattr(mod, "DOCKERFILE_APT", None) or []):
            if pkg not in seen_apt:
                apt_pkgs.append(pkg); seen_apt.add(pkg)
        for cmd in (getattr(mod, "DOCKERFILE_RUN", None) or []):
            if cmd not in seen_run:
                run_cmds.append(cmd); seen_run.add(cmd)
        for pkg in (getattr(mod, "DOCKERFILE_PIP", None) or []):
            if pkg not in seen_pip:
                pip_pkgs.append(pkg); seen_pip.add(pkg)

    return {"apt": apt_pkgs, "run": run_cmds, "pip": pip_pkgs}

def _build_dockerfile(deps: dict) -> str:
    """يقرأ Dockerfile.base ويستبدل placeholders بمحتوى من الـ plugins."""
    if not os.path.exists(DOCKERFILE_BASE):
        logger.error("❌ Dockerfile.base غير موجود!")
        return ""

    with open(DOCKERFILE_BASE) as f:
        content = f.read()

    # ── {PLUGIN_APT_BLOCK} ──
    if deps["apt"]:
        pkgs_str = " \\\n        ".join(deps["apt"])
        apt_block = (
            f"# حزم apt مُضافة تلقائياً من الـ plugins\n"
            f"RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"        {pkgs_str} \\\n"
            f"    && apt-get clean \\\n"
            f"    && rm -rf /var/lib/apt/lists/*"
        )
    else:
        apt_block = "# (لا توجد حزم apt إضافية من الـ plugins)"

    # ── {PLUGIN_RUN_BLOCK} ──
    if deps["run"]:
        run_block = "\n".join(f"RUN {cmd}" for cmd in deps["run"])
        run_block = "# أوامر إضافية من الـ plugins\n" + run_block
    else:
        run_block = "# (لا توجد أوامر RUN إضافية من الـ plugins)"

    # ── {PLUGIN_PIP_BLOCK} ──
    if deps["pip"]:
        pkgs_str  = " \\\n    ".join(deps["pip"])
        pip_block = (
            f"# حزم pip مُضافة تلقائياً من الـ plugins\n"
            f"RUN pip install --no-cache-dir \\\n"
            f"    {pkgs_str}"
        )
    else:
        pip_block = "# (لا توجد حزم pip إضافية من الـ plugins)"

    content = content.replace(
        "# ── {PLUGIN_APT_BLOCK} ── حزم apt من الـ plugins (يُولَّد تلقائياً) ──",
        apt_block
    )
    content = content.replace(
        "# ── {PLUGIN_RUN_BLOCK} ── أوامر RUN إضافية من الـ plugins ──────",
        run_block
    )
    content = content.replace(
        "# ── {PLUGIN_PIP_BLOCK} ── pip install إضافية من الـ plugins ────",
        pip_block
    )

    return content

def _sync_dockerfile(modules: list):
    """يولّد Dockerfile من Dockerfile.base + deps — فقط إذا تغيّر المحتوى."""
    deps    = _collect_dockerfile_deps(modules)
    content = _build_dockerfile(deps)
    if not content:
        return

    # تجنب إعادة الكتابة إذا لم يتغير شيء (بناءً على hash)
    new_hash = hashlib.md5(content.encode()).hexdigest()
    old_hash = ""
    if os.path.exists(DOCKERFILE_OUT):
        with open(DOCKERFILE_OUT) as f:
            old_hash = hashlib.md5(f.read().encode()).hexdigest()

    if new_hash == old_hash:
        logger.info("[dockerfile] ✅ Dockerfile محدَّث بالفعل — لا تغيير")
        return

    with open(DOCKERFILE_OUT, "w") as f:
        f.write(content)

    summary = (
        f"apt({len(deps['apt'])}): {deps['apt']} | "
        f"run({len(deps['run'])}): {deps['run']} | "
        f"pip({len(deps['pip'])}): {deps['pip']}"
    )
    logger.info(f"[dockerfile] ✅ تم توليد Dockerfile — {summary}")

# ══════════════════════════════════════════════
# تحميل الـ plugins
# ══════════════════════════════════════════════

def load_all_plugins():
    """نقطة الدخول الرئيسية — تُستدعى مرة واحدة من main.py عند الإقلاع."""
    os.makedirs(PLUGINS_DIR, exist_ok=True)

    files = sorted(glob.glob(os.path.join(PLUGINS_DIR, "*.py")))
    files = [f for f in files if not os.path.basename(f).startswith("_")]

    loaded_modules = []
    loaded = failed = 0

    for fpath in files:
        name = os.path.basename(fpath)[:-3]
        mod  = _load_one(name, fpath)
        if mod:
            loaded_modules.append(mod)
            loaded += 1
        else:
            failed += 1

    # ترتيب حسب PRIORITY
    _plugins.sort(key=lambda p: p.get("priority", 50))
    _search_providers.sort(key=lambda p: p.get("priority", 50))

    # توليد Dockerfile ديناميكياً
    _sync_dockerfile(loaded_modules)

    logger.info(
        f"[plugin_loader] ✅ {loaded} plugin محمَّل"
        + (f" | ❌ {failed} فشل" if failed else "")
        + f" | الترتيب: {[p['name'] for p in _plugins]}"
    )

async def run_pending_setups():
    """يُنتظر (await) من main.py بعد load_all_plugins() — ينفّذ setup() كل
    الـ plugins بالتوازي وينتظر اكتمالها قبل بدء استقبال الطلبات فعلياً."""
    import asyncio
    if not _pending_setups:
        return
    async def _run(name, fn):
        try:
            await fn()
        except Exception as e:
            logger.warning(f"[{name}] setup() فشل: {e}")
    await asyncio.gather(*(_run(name, fn) for name, fn in _pending_setups))
    logger.info(f"[plugin_loader] ✅ اكتمل setup() لـ {len(_pending_setups)} plugin")

def _load_one(name: str, fpath: str):
    """يحمل plugin واحد — يرجع module عند النجاح، None عند الفشل."""
    try:
        spec   = importlib.util.spec_from_file_location(f"plugins.{name}", fpath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"plugins.{name}"] = module
        spec.loader.exec_module(module)

        domains = getattr(module, "DOMAINS", [])
        is_url_plugin     = bool(domains) and hasattr(module, "probe") and hasattr(module, "download")
        is_handler_plugin = hasattr(module, "register_plugin")
        is_search_plugin  = hasattr(module, "search")

        if not is_url_plugin and not is_handler_plugin and not is_search_plugin:
            missing = [fn for fn in ("probe", "download") if not hasattr(module, fn)]
            logger.warning(f"[{name}] ⚠️ لا probe/download/DOMAINS ولا register_plugin ولا search — تخطي")
            _registry[name] = {"status": "skipped", "reason": f"missing: {missing}, no register_plugin/search"}
            return None

        if is_url_plugin:
            entry = {
                "name":        name,
                "module":      module,
                "domains":     domains,
                "priority":    getattr(module, "PRIORITY",    50),
                "description": getattr(module, "DESCRIPTION", ""),
                "probe":       module.probe,
                "download":    module.download,
            }
            _plugins.append(entry)

        if is_search_plugin:
            _search_providers.append({
                "name":     name,
                "search":   module.search,
                "priority": getattr(module, "SEARCH_PRIORITY", 50),
            })

        handler_count = 0
        if is_handler_plugin:
            try:
                handlers = module.register_plugin()
                if handlers and not isinstance(handlers, (list, tuple)):
                    handlers = [handlers]
                for h in (handlers or []):
                    _extra_handlers.append(h)
                handler_count = len(handlers or [])
            except Exception:
                logger.exception(f"[{name}] register_plugin() فشل")

        # setup() اختياري — يُخزَّن ليُنفَّذ لاحقاً بانتظار حقيقي (await)
        # من load_all_plugins_async بدل create_task() غير المنتظر، حتى لا
        # يبدأ السيرفر استقبال الطلبات قبل اكتمال setup() لأي plugin
        if hasattr(module, "setup"):
            _pending_setups.append((name, module.setup))

        # سجّل deps للعرض
        apt = getattr(module, "DOCKERFILE_APT", []) or []
        run = getattr(module, "DOCKERFILE_RUN", []) or []
        pip = getattr(module, "DOCKERFILE_PIP", []) or []

        types = []
        if is_url_plugin:     types.append("رابط")
        if is_handler_plugin: types.append("مباشر")
        if is_search_plugin:  types.append("بحث")

        _registry[name] = {
            "status":      "loaded",
            "type":        "+".join(types),
            "domains":     domains,
            "priority":    getattr(module, "PRIORITY", 50),
            "description": getattr(module, "DESCRIPTION", ""),
            "handlers":    handler_count,
            "search":      is_search_plugin,
            "dockerfile":  {
                "apt": apt, "run": run, "pip": pip,
                "has_deps": bool(apt or run or pip),
            },
        }
        logger.info(
            f"[{name}] ✅ محمَّل | نوع={'+'.join(types)}"
            + (f" | priority={getattr(module,'PRIORITY',50)}" if is_url_plugin else "")
            + (f" | handlers={handler_count}" if is_handler_plugin else "")
            + (f" | search_priority={getattr(module,'SEARCH_PRIORITY',50)}" if is_search_plugin else "")
            + (f" | apt={apt}" if apt else "")
            + (f" | run={run}" if run else "")
            + (f" | pip={pip}" if pip else "")
        )
        return module

    except Exception as e:
        logger.exception(f"[{name}] ❌ فشل: {e}")
        _registry[name] = {"status": "error", "reason": str(e)}
        return None
