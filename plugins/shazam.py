# -*- coding: utf-8 -*-
"""
plugins/shazam.py
==================
وحدة تعرّف عبر Shazam — لا تُسجَّل كـ handler مباشر بعد الآن. استقبال
الوسائط المُرسلة مباشرة (voice/audio/video/video_note) وعرض قائمة
الخيارات (شازام/كلمات/تحويل لصوت/قص بالزمن) أصبح مسؤولية
plugins/media_tools.py، والذي يستدعي analyze_audio_shazam() أدناه
عند اختيار المستخدم لخيار "اكتشاف الأغنية".

يوفّر أيضاً identify_from_message() القابلة للاستيراد من plugins أخرى
(مثل plugins/lyrics.py و plugins/media_tools.py) لإعادة استخدام نفس
منطق التعرّف بدل تكراره.
"""
import os, sys, asyncio, subprocess, importlib, logging, tempfile

logger = logging.getLogger("plugin.shazam")

DESCRIPTION = "التعرف على الأغاني من ملفات صوت/فيديو مُرسلة مباشرة — Shazam"

DOCKERFILE_PIP = ["shazamio"]

try:
    importlib.import_module("shazamio")
except ImportError:
    logger.warning("[shazam] shazamio غير مثبتة، جاري التثبيت الديناميكي (يفضّل إعادة بناء الصورة)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "shazamio"])

from shazamio import Shazam as ShazamClient

_shazam_client = None

def _get_client() -> ShazamClient:
    global _shazam_client
    if _shazam_client is None:
        _shazam_client = ShazamClient()
    return _shazam_client

MAX_ANALYZE_SIZE = 45 * 1024 * 1024  # 45MB


def _media_and_suffix(msg: dict):
    """يرجع (media_dict, file_extension) لأول وسائط قابلة للتعرّف في الرسالة، أو (None, None)."""
    if "voice" in msg:      return msg["voice"], ".ogg"
    if "video_note" in msg: return msg["video_note"], ".mp4"
    if "video" in msg:      return msg["video"], ".mp4"
    if "audio" in msg:
        a  = msg["audio"]
        fn = a.get("file_name") or ""
        ext = os.path.splitext(fn)[1]
        return a, (ext if ext else ".mp3")
    return None, None


async def identify_from_message(msg: dict, bot) -> dict:
    """يحمّل وسائط الرسالة ويتعرّف عليها عبر Shazam.
    يرجع {"title":..., "artist":..., "url":..., "cover":...} أو يرمي استثناء."""
    media, suffix = _media_and_suffix(msg)
    if not media:
        raise Exception("لا توجد وسائط صوت/فيديو قابلة للتعرف في هذه الرسالة")

    if media.get("file_size") and media["file_size"] > MAX_ANALYZE_SIZE:
        raise Exception(
            f"حجم الملف ({media['file_size']/1024/1024:.1f}MB) كبير جداً للتحليل "
            f"(الحد الأقصى {MAX_ANALYZE_SIZE/1024/1024:.0f}MB)."
        )

    fd, temp_file_path = tempfile.mkstemp(suffix=suffix, prefix="shazam_")
    os.close(fd)
    try:
        await bot.download_file(media["file_id"], temp_file_path)
        result = await asyncio.wait_for(_get_client().recognize(temp_file_path), timeout=30)
        track = result.get("track") if isinstance(result, dict) else None
        if not track:
            raise Exception("لم يُعثر على تطابق لهذه الأغنية")
        return {
            "title":  track.get("title", "غير معروف"),
            "artist": track.get("subtitle", "غير معروف"),
            "url":    track.get("url", ""),
            "cover":  ((track.get("images") or {}).get("coverart")) or "",
        }
    finally:
        try:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
        except Exception:
            logger.exception(f"[shazam] فشل حذف الملف المؤقت: {temp_file_path}")


async def analyze_audio_shazam(msg: dict, bot):
    """المستقبل الخاص بملفات الصوت/الفيديو المُرسلة مباشرة — يرد بنتيجة التعرف."""
    chat_id = msg["chat"]["id"]
    status  = await bot.send_message(chat_id, "⏳ جاري سحب البصمة الصوتية وتحليلها عبر شازام...")

    try:
        track = await identify_from_message(msg, bot)
    except Exception as e:
        logger.exception(f"[shazam] فشل التحليل | chat={chat_id}")
        await bot.edit_message_text(chat_id, status["message_id"], f"⚠️ {str(e)[:300]}")
        return

    reply_text = (
        "🎵 تم التعرف على الأغنية بنجاح!\n\n"
        f"📌 العنوان: {track['title']}\n"
        f"🎤 الفنان: {track['artist']}"
    )
    if track["url"]:
        reply_text += f"\n🔗 {track['url']}"

    try:
        if track["cover"]:
            await bot.delete_message(chat_id, status["message_id"])
            await bot.send_photo(chat_id, track["cover"], caption=reply_text)
        else:
            await bot.edit_message_text(chat_id, status["message_id"], reply_text)
    except Exception:
        logger.exception("[shazam] فشل إرسال صورة الغلاف — إرسال نص فقط")
        await bot.send_message(chat_id, reply_text)


def register_plugin():
    """لم تعد Shazam تُسجَّل كـ handler مباشر للوسائط — plugins/media_tools.py
    يستدعي analyze_audio_shazam()/identify_from_message() مباشرة عند اختيار
    المستخدم لذلك من القائمة. نُبقي هذه الدالة (بلا handlers) فقط ليبقى
    الملف مصنَّفاً كـ plugin مُحمَّل طبيعياً في سجل /plugins بدل "متخطّى"."""
    return []

