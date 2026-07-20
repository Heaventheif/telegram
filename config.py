# -*- coding: utf-8 -*-
"""
config.py — مصدر واحد للحقيقة لكل متغيرات البيئة
====================================================
يمركز كل التوكنات، روابط الـ APIs الخارجية، إعدادات الـ webhook، وإعدادات
الكاش في مكان واحد بدل تشتّتها عبر os.getenv() في main.py و plugins/*.py.

يُحمَّل ملف .env تلقائياً (عبر python-dotenv) إن وُجد — مفيد محلياً؛ في
الإنتاج (Render/Docker) تُضبط المتغيرات مباشرة من لوحة التحكم/docker-compose
ويُتجاهَل غياب .env بصمت.

الاستخدام من أي مكان في المشروع:
    from config import config
    config.TELEGRAM_TOKEN
    config.MAX_CONCURRENT_DOWNLOADS
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()  # لا يفعل شيئاً إن لم يوجد ملف .env — آمن في كل البيئات

logger = logging.getLogger("config")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # ══════════════════════════════════════════
    # 🤖 Telegram / Webhook
    # ══════════════════════════════════════════
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
    SERVER_URL:     str = os.getenv("SERVER_URL", "").rstrip("/")
    PORT:           int = int(os.getenv("PORT", "10000"))
    WEBHOOK_PATH:   str = os.getenv("WEBHOOK_PATH", "/webhook")

    # ══════════════════════════════════════════
    # ⚙️ حدود التشغيل العامة
    # ══════════════════════════════════════════
    MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
    UPLOAD_LIMIT_MB:          int = int(os.getenv("UPLOAD_LIMIT_MB", "50"))
    UPLOAD_LIMIT:             int = UPLOAD_LIMIT_MB * 1024 * 1024

    PENDING_TTL_MIN:        int = int(os.getenv("PENDING_TTL_MIN", "30"))
    SEARCH_PENDING_TTL_MIN: int = int(os.getenv("SEARCH_PENDING_TTL_MIN", "15"))

    # ══════════════════════════════════════════
    # 🗄️ طبقة الكاش (media_cache) — راجع cache.py
    #    الافتراضي: SQLite محلي بدون أي بنية تحتية خارجية.
    #    عند ضبط REDIS_URL يُستخدم Redis تلقائياً (أفضل لتوسّع أفقي
    #    متعدد النسخ — راجع الشرح في README.md).
    # ══════════════════════════════════════════
    CACHE_DB_PATH:    str  = os.getenv("CACHE_DB_PATH", "media_cache.db")
    REDIS_URL:        str  = os.getenv("REDIS_URL", "").strip() or None
    CACHE_TTL_DAYS:   int  = int(os.getenv("CACHE_TTL_DAYS", "30"))
    CACHE_TTL:        int  = CACHE_TTL_DAYS * 86400 if CACHE_TTL_DAYS > 0 else None
    CACHE_HASH_LEN:   int  = int(os.getenv("CACHE_HASH_LEN", "8"))  # طول الهاش القصير للرابط
    CACHE_ENABLED:    bool = _bool("CACHE_ENABLED", True)

    # ══════════════════════════════════════════
    # 🔌 مفاتيح/روابط الـ APIs الخارجية المستخدمة من الـ plugins
    # ══════════════════════════════════════════
    YT_API_1:        str = os.getenv("YT_API_1", "https://ccproject.serv00.net/ytdl2.php")
    YT_API_2:        str = os.getenv("YT_API_2", "https://yt-dlp-stream.onrender.com/api")
    FB_DOWNLOAD_API: str = os.getenv("FB_DOWNLOAD_API", "https://facebook-video-download-api.onrender.com")
    LYRICS_API:      str = os.getenv("LYRICS_API", "https://api.lyrics.ovh/v1")
    GROQ_API_KEY:    str = os.getenv("GROQ_API_KEY", "")  # لتفريغ الصوت/الفيديو نصياً (VTT) عبر Groq Whisper

    # ══════════════════════════════════════════
    # 📝 تسجيل الأحداث (logging)
    # ══════════════════════════════════════════
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @classmethod
    def validate(cls) -> None:
        """يتحقق من وجود المتغيرات الإلزامية عند الإقلاع — يوقف العملية برسالة واضحة إن نقصت."""
        missing = [name for name in ("TELEGRAM_TOKEN", "SERVER_URL") if not getattr(cls, name)]
        if missing:
            logger.error(f"❌ متغيرات بيئة إلزامية ناقصة: {missing} — راجع .env.example")
            raise SystemExit(1)

        if cls.MAX_CONCURRENT_DOWNLOADS < 1:
            logger.warning("⚠️ MAX_CONCURRENT_DOWNLOADS < 1 — سيُستخدم 1 كحد أدنى آمن")
            cls.MAX_CONCURRENT_DOWNLOADS = 1


config = Config()
