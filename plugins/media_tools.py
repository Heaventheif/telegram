# -*- coding: utf-8 -*-
"""
plugins/media_tools.py
========================
Handler-plugin: عندما يرسل المستخدم وسائط مباشرة (صوت/فيديو/ملاحظة فيديو)
بدون رابط، تظهر قائمة أزرار بدل التحليل الفوري:

    🎵 اكتشاف الأغنية (Shazam)
    📝 عرض الكلمات (Lyrics)
    📝 إنشاء ترجمة (VTT)          ← تفريغ نصي بالتوقيت عبر Groq Whisper (Bun worker)
    🔄 تحويل الفيديو إلى صوت     ← فقط لرسائل فيديو/video_note
    ✂️ قص جزء محدد بالزمن         ← فقط لرسائل فيديو/video_note

يعيد استخدام plugins.shazam.identify_from_message()/analyze_audio_shazam()
للتعرف، ويستدعي _reply_with_lyrics() من plugins.lyrics لعرض الكلمات، ويستخدم
ffmpeg محلياً (نفس الطريقة المستخدمة في plugin_loader.split_media) للتحويل
والقص.

trim_media_by_time() و parse_time_range() معروضتان أيضاً للاستيراد من
main.py لإعادة استخدامهما في مسار "تنزيل جزء من رابط" (راجع main.py).
"""
import os, re, json, time, asyncio, logging, tempfile, shutil
from telegram_api import is_recognizable_media
import cache
from config import config

logger = logging.getLogger("plugin.media_tools")

DESCRIPTION = "قائمة أدوات للوسائط المباشرة: شازام، كلمات، تحويل لصوت، قص جزء بالزمن، ترجمة VTT"

_TTL = config.PENDING_TTL_MIN * 60

# ── تفريغ نصي (VTT) عبر Bun worker + Groq Whisper ──
# يشارك مجلد plugins/_js/ مع plugins/youtube.py، الذي يتولى وحده تشغيل
# `bun install` عند الإقلاع (راجع setup() هناك) لتفادي تضارب تثبيت متزامن
# لو كل بلجن ثبّت تبعياته بنفسه.
_JS_DIR             = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_js")
_TRANSCRIBE_WORKER  = os.path.join(_JS_DIR, "transcribe_worker.js")
_BUN_BIN            = shutil.which(os.getenv("BUN_BIN", "bun")) or os.getenv("BUN_BIN", "bun")
_GROQ_API_KEY       = config.GROQ_API_KEY

# token -> {"msg": dict, "ts": float}                 (الوسائط المُستلَمة بانتظار اختيار المستخدم)
MEDIA_PENDING = {}
# chat_id -> {"token": str, "status_message_id": int, "ts": float}  (بانتظار رد نصي بالوقت لقص فيديو مُرسَل مباشرة)
CLIP_AWAIT = {}


def _cleanup():
    now = time.time()
    for k in [k for k, v in MEDIA_PENDING.items() if now - v["ts"] > _TTL]:
        MEDIA_PENDING.pop(k, None)
    for k in [k for k, v in CLIP_AWAIT.items() if now - v["ts"] > _TTL]:
        CLIP_AWAIT.pop(k, None)


def _media_and_suffix(msg: dict):
    """يرجع (media_dict, file_extension, kind) لأول وسائط قابلة للتعرّف في
    الرسالة، أو (None, None, None). kind ∈ {"voice","video_note","video","audio"}."""
    if "voice" in msg:      return msg["voice"], ".ogg", "voice"
    if "video_note" in msg: return msg["video_note"], ".mp4", "video_note"
    if "video" in msg:      return msg["video"], ".mp4", "video"
    if "audio" in msg:
        a   = msg["audio"]
        fn  = a.get("file_name") or ""
        ext = os.path.splitext(fn)[1]
        return a, (ext if ext else ".mp3"), "audio"
    return None, None, None


def _is_video_kind(kind: str) -> bool:
    return kind in ("video", "video_note")


def _cleanup_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.exception(f"[media_tools] فشل حذف الملف المؤقت: {path}")


# ══════════════════════════════════════════════
# 📋 عرض القائمة عند استقبال وسائط مباشرة
# ══════════════════════════════════════════════
async def show_media_menu(msg: dict, bot):
    chat_id = msg["chat"]["id"]
    media, _, kind = _media_and_suffix(msg)
    if not media:
        return

    _cleanup()
    token = cache.short_hash(f"{chat_id}|{msg.get('message_id')}|{time.time()}", config.CACHE_HASH_LEN)
    MEDIA_PENDING[token] = {"msg": msg, "ts": time.time()}

    rows = [
        [{"text": "🎵 اكتشاف الأغنية (Shazam)", "callback_data": f"mtool|{token}|shazam"}],
        [{"text": "📝 عرض الكلمات (Lyrics)",     "callback_data": f"mtool|{token}|lyrics"}],
        [{"text": "📝 إنشاء ترجمة (VTT)",         "callback_data": f"mtool|{token}|vtt"}],
    ]
    if _is_video_kind(kind):
        rows.append([{"text": "🔄 تحويل الفيديو إلى صوت", "callback_data": f"mtool|{token}|to_audio"}])
        rows.append([{"text": "✂️ قص جزء محدد بالزمن",     "callback_data": f"mtool|{token}|cut"}])

    await bot.send_message(
        chat_id,
        "📎 استلمت الوسائط! ماذا تريد أن تفعل بها؟",
        reply_markup={"inline_keyboard": rows},
    )


# ══════════════════════════════════════════════
# ⬇️ تحميل الوسائط الأصلية من تيليجرام إلى ملف مؤقت
# ══════════════════════════════════════════════
async def _download_original(bot, msg: dict) -> tuple:
    media, suffix, kind = _media_and_suffix(msg)
    if not media:
        raise Exception("لا توجد وسائط في هذه الرسالة")
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="mtool_")
    os.close(fd)
    await bot.download_file(media["file_id"], path)
    return path, kind


# ══════════════════════════════════════════════
# 🔄 تحويل فيديو → صوت (mp3) عبر ffmpeg
# ══════════════════════════════════════════════
async def video_to_audio(src_path: str) -> str:
    fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix="mtool_audio_")
    os.close(fd)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        _cleanup_file(out_path)
        raise Exception(f"فشل التحويل عبر ffmpeg: {stderr.decode(errors='ignore')[:300]}")
    return out_path


# ══════════════════════════════════════════════
# ⏱️ تحليل صيغة "البداية-النهاية" النصية إلى ثوانٍ
# يدعم فقط: mm:ss  مثل  0:30  أو  12:05
# ══════════════════════════════════════════════
_SEP_RE  = re.compile(r"\s*(?:-|–|—|إلى|الى|to)\s*", re.IGNORECASE)
_MMSS_RE = re.compile(r"^(\d{1,3}):([0-5]?\d)$")


def _to_seconds(t: str) -> float:
    t = t.strip()
    m = _MMSS_RE.match(t)
    if not m:
        raise ValueError(f"صيغة وقت غير صالحة: «{t}» — استخدم mm:ss فقط، مثال: 0:30")
    minutes, seconds = int(m.group(1)), int(m.group(2))
    return minutes * 60 + seconds


def format_seconds(s: float) -> str:
    """ينسّق ثوانٍ عائمة إلى mm:ss للعرض على المستخدم."""
    s_int = max(0, round(s))
    m, sec = divmod(s_int, 60)
    return f"{m}:{sec:02d}"


def parse_time_range(text: str):
    """يحلّل نصاً مثل '0:30-1:15' إلى (start_seconds, end_seconds) — صيغة
    mm:ss فقط لكل من البداية والنهاية. يرمي ValueError إن كانت الصيغة غير
    صالحة، أو start >= end، أو تجاوزت المدة الحد الأقصى المسموح
    (MAX_CLIP_DURATION_SECONDS) — يمنع طلب قص جزء طويل جداً يستهلك
    CPU/RAM/قرص بلا داعٍ (مثلاً '0:00-999:99')."""
    parts = [p for p in _SEP_RE.split((text or "").strip(), maxsplit=1) if p != ""]
    if len(parts) != 2:
        raise ValueError("استخدم صيغة: البداية-النهاية بصيغة mm:ss، مثال: 0:30-1:15")
    start, end = _to_seconds(parts[0]), _to_seconds(parts[1])
    if start < 0 or end <= start:
        raise ValueError("يجب أن تكون نهاية المقطع بعد بدايته")
    max_dur = config.MAX_CLIP_DURATION_SECONDS
    if max_dur and (end - start) > max_dur:
        raise ValueError(
            f"الجزء المطلوب طويل جداً ({format_seconds(end - start)}) — "
            f"الحد الأقصى المسموح {format_seconds(max_dur)}"
        )
    return start, end


# ══════════════════════════════════════════════
# ✂️ قص جزء من ملف صوت/فيديو بالزمن عبر ffmpeg
# (يُستخدم من هنا لقص وسائط مُرسلة مباشرة، ومن main.py لقص رابط تم اختيار
#  "تنزيل جزء" له — دالة عامة واحدة بدل تكرار المنطق في مكانين)
# ══════════════════════════════════════════════
async def trim_media_by_time(src_path: str, start: float, end: float, is_audio: bool = False) -> str:
    duration = end - start
    suffix   = os.path.splitext(src_path)[1] or (".mp3" if is_audio else ".mp4")
    fd, out_path = tempfile.mkstemp(suffix=suffix, prefix="mtool_cut_")
    os.close(fd)

    async def _run(*codec_args):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", src_path,
            *codec_args, out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        ok = proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
        return ok, stderr

    # نجرّب أولاً نسخ الترميز (-c copy) لأنه سريع جداً — قد يفشل إن لم يقع
    # وقت البداية على keyframe بالضبط، فنعيد الترميز تلقائياً عند الفشل.
    ok, stderr = await _run("-c", "copy")
    if not ok:
        codec_args = ("-c:a", "libmp3lame", "-q:a", "2") if is_audio else ("-c:v", "libx264", "-c:a", "aac")
        ok, stderr = await _run(*codec_args)
    if not ok:
        _cleanup_file(out_path)
        raise Exception(f"فشل قص المقطع عبر ffmpeg: {stderr.decode(errors='ignore')[:300]}")
    return out_path


# ══════════════════════════════════════════════
# 📝 تفريغ نصي (VTT) عبر Bun worker + Groq Whisper
# ══════════════════════════════════════════════
def _seconds_to_vtt_ts(t: float) -> str:
    t = max(0.0, float(t))
    h, rem = divmod(t, 3600)
    m, s   = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _segments_to_vtt(segments: list) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{_seconds_to_vtt_ts(seg.get('start', 0))} --> {_seconds_to_vtt_ts(seg.get('end', 0))}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


async def transcribe_to_vtt(src_path: str) -> str:
    """يفرّغ ملف صوت/فيديو عبر Groq Whisper (transcribe_worker.js) ويرجع
    مسار ملف .vtt مؤقت جاهز للإرسال."""
    if not _GROQ_API_KEY:
        raise Exception("GROQ_API_KEY غير مضبوط في متغيرات البيئة")

    try:
        proc = await asyncio.create_subprocess_exec(
            _BUN_BIN, "run", _TRANSCRIBE_WORKER, src_path,
            cwd=_JS_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=180)
    except FileNotFoundError:
        raise Exception("الأمر bun غير مثبَّت — راجع DOCKERFILE_RUN في plugins/youtube.py")
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        raise Exception("انتهت مهلة التفريغ النصي (180ث)")

    lines = [l for l in out_b.decode(errors="ignore").splitlines() if l.strip()]
    if not lines:
        raise Exception(f"لا يوجد إخراج من worker التفريغ | stderr={err_b.decode(errors='ignore')[:200]}")
    try:
        data = json.loads(lines[-1])
    except Exception:
        raise Exception(f"استجابة غير صالحة من worker التفريغ | {lines[-1][:200]}")
    if not data.get("status"):
        raise Exception(data.get("error", "فشل التفريغ النصي"))

    vtt_text = _segments_to_vtt(data.get("segments") or [])
    fd, vtt_path = tempfile.mkstemp(suffix=".vtt", prefix="mtool_vtt_")
    os.close(fd)
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write(vtt_text)
    return vtt_path


# ══════════════════════════════════════════════
# 🎛️ معالج ضغط أزرار القائمة (callback_query بادئتها mtool|)
# ══════════════════════════════════════════════
def is_mtool_callback(obj: dict) -> bool:
    return "data" in obj and (obj.get("data") or "").startswith("mtool|")


async def handle_mtool_callback(cq: dict, bot):
    msg_container = cq["message"]
    chat_id       = msg_container["chat"]["id"]
    status_id     = msg_container["message_id"]
    data          = cq.get("data") or ""

    try:
        _, token, action = data.split("|", 2)
    except ValueError:
        await bot.answer_callback_query(cq["id"], "⚠️ طلب غير صالح.", show_alert=True)
        return

    _cleanup()
    entry = MEDIA_PENDING.get(token)
    if not entry:
        await bot.answer_callback_query(cq["id"], "⌛ انتهت صلاحية الطلب، أعد إرسال الوسائط.", show_alert=True)
        return

    await bot.answer_callback_query(cq["id"])
    original_msg = entry["msg"]
    _, _, kind   = _media_and_suffix(original_msg)

    if action == "shazam":
        import plugins.shazam as shazam_mod
        await bot.delete_message(chat_id, status_id)
        await shazam_mod.analyze_audio_shazam(original_msg, bot)
        MEDIA_PENDING.pop(token, None)
        return

    if action == "lyrics":
        import plugins.shazam as shazam_mod
        from plugins.lyrics import _reply_with_lyrics
        await bot.edit_message_text(chat_id, status_id, "⏳ جاري التعرف على المقطع...")
        try:
            track = await shazam_mod.identify_from_message(original_msg, bot)
        except Exception as e:
            await bot.edit_message_text(chat_id, status_id, f"❌ تعذّر التعرف على المقطع:\n{str(e)[:200]}")
            return
        await bot.edit_message_text(chat_id, status_id, "🔍 جاري البحث عن الكلمات...")
        await _reply_with_lyrics(bot, chat_id, status_id, track["artist"], track["title"])
        MEDIA_PENDING.pop(token, None)
        return

    if action == "to_audio":
        if not _is_video_kind(kind):
            await bot.edit_message_text(chat_id, status_id, "❌ هذا الخيار متاح فقط للفيديو.")
            return
        await bot.edit_message_text(chat_id, status_id, "⏳ جاري تحميل الفيديو وتحويله إلى صوت...")
        src = out = None
        try:
            src, _ = await _download_original(bot, original_msg)
            out = await video_to_audio(src)
            await bot.edit_message_text(chat_id, status_id, "📤 جاري رفع الملف الصوتي...")
            await bot.send_audio(chat_id, out, title="مقطع صوتي")
            await bot.delete_message(chat_id, status_id)
        except Exception as e:
            logger.exception("[media_tools] فشل التحويل إلى صوت")
            await bot.edit_message_text(chat_id, status_id, f"❌ فشل التحويل:\n{str(e)[:200]}")
        finally:
            _cleanup_file(src); _cleanup_file(out)
        MEDIA_PENDING.pop(token, None)
        return

    if action == "vtt":
        await bot.edit_message_text(chat_id, status_id, "⏳ جاري تحميل الوسائط...")
        src = vtt_path = None
        try:
            src, _ = await _download_original(bot, original_msg)
            await bot.edit_message_text(chat_id, status_id, "🧠 جاري تفريغ النص عبر Groq Whisper...")
            vtt_path = await transcribe_to_vtt(src)
            await bot.edit_message_text(chat_id, status_id, "📤 جاري رفع ملف الترجمة...")
            await bot.send_document(chat_id, vtt_path, filename="captions.vtt", caption=None)
            await bot.delete_message(chat_id, status_id)
        except Exception as e:
            logger.exception("[media_tools] فشل إنشاء الترجمة")
            await bot.edit_message_text(chat_id, status_id, f"❌ فشل إنشاء الترجمة:\n{str(e)[:200]}")
        finally:
            _cleanup_file(src); _cleanup_file(vtt_path)
        MEDIA_PENDING.pop(token, None)
        return

    if action == "cut":
        if not _is_video_kind(kind):
            await bot.edit_message_text(chat_id, status_id, "❌ هذا الخيار متاح فقط للفيديو.")
            return
        await bot.edit_message_text(
            chat_id, status_id,
            "⏱️ أرسل وقت الجزء المطلوب بصيغة (البداية-النهاية) mm:ss\n"
            "مثال: `0:30-1:15`",
            parse_mode="Markdown",
        )
        CLIP_AWAIT[chat_id] = {"token": token, "status_message_id": status_id, "ts": time.time()}
        return

    await bot.answer_callback_query(cq["id"], "⚠️ خيار غير معروف.", show_alert=True)


# ══════════════════════════════════════════════
# ⌨️ استقبال رد نصي بالوقت لتنفيذ قص وسائط مُرسلة مباشرة
# ══════════════════════════════════════════════
def is_awaiting_clip_reply(msg: dict) -> bool:
    if "data" in msg:  # هذا callback_query وليس رسالة نصية — تجاهل
        return False
    if "text" not in msg or (msg.get("text") or "").startswith("/"):
        return False
    chat_id = (msg.get("chat") or {}).get("id")
    return chat_id in CLIP_AWAIT


async def handle_clip_time_reply(msg: dict, bot):
    chat_id = msg["chat"]["id"]
    pending = CLIP_AWAIT.pop(chat_id, None)
    if not pending:
        return

    entry = MEDIA_PENDING.get(pending["token"])
    if not entry:
        await bot.send_message(chat_id, "⌛ انتهت صلاحية الطلب، أعد إرسال الفيديو.")
        return

    try:
        start, end = parse_time_range(msg.get("text", ""))
    except ValueError as e:
        await bot.send_message(chat_id, f"❌ {e}")
        CLIP_AWAIT[chat_id] = pending  # أعد الانتظار ليحاول المستخدم مجدداً
        return

    status_id = pending["status_message_id"]
    # 1) تنزيل الوسائط الأصلية كاملة أولاً من تيليجرام إلى القرص
    await bot.edit_message_text(chat_id, status_id, "⏳ جاري تنزيل الوسائط كاملة...")

    src = out = None
    try:
        src, _        = await _download_original(bot, entry["msg"])
        _, _, kind    = _media_and_suffix(entry["msg"])
        # 2) بعد اكتمال التنزيل الكامل فقط — نقصّ الجزء المطلوب منه محلياً
        await bot.edit_message_text(
            chat_id, status_id,
            f"✂️ جاري قص الجزء المطلوب ({format_seconds(start)} → {format_seconds(end)})..."
        )
        out = await trim_media_by_time(src, start, end, is_audio=False)
        await bot.edit_message_text(chat_id, status_id, "📤 جاري رفع المقطع المقصوص...")
        await bot.send_video(chat_id, out, caption=None)
        await bot.delete_message(chat_id, status_id)
    except Exception as e:
        logger.exception("[media_tools] فشل قص المقطع")
        await bot.edit_message_text(chat_id, status_id, f"❌ فشل قص المقطع:\n{str(e)[:200]}")
    finally:
        _cleanup_file(src); _cleanup_file(out)
        MEDIA_PENDING.pop(pending["token"], None)


def register_plugin():
    return [
        {"filter": is_recognizable_media, "callback": show_media_menu},
        {"filter": is_mtool_callback,      "callback": handle_mtool_callback},
        {"filter": is_awaiting_clip_reply, "callback": handle_clip_time_reply},
    ]
