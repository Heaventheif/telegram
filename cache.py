# -*- coding: utf-8 -*-
"""
cache.py — طبقة كاش الوسائط: هاش قصير للرابط ↔ Telegram file_id
====================================================================
لماذا هذا التصميم؟
-------------------
Telegram يخزّن كل ملف تم رفعه على خوادمه ويعطيه `file_id` دائم — إعادة
إرسال نفس `file_id` فوري (لا رفع فعلي، لا استهلاك باندويدث) بعكس تحميل
الرابط الأصلي وتقسيمه ورفعه من جديد. لذلك: أول طلب لرابط معيّن بجودة
معيّنة يُحمَّل ويُرفع بشكل طبيعي ثم يُخزَّن `file_id` الناتج، وأي طلب
لاحق لنفس الرابط + نفس الجودة يُعاد إرساله فوراً من الكاش.

نستخدم **هاش قصير (8 أحرف افتراضياً) من SHA-256 للرابط** كمفتاح بدل
الرابط الكامل لسببين:
  1) نفس المفتاح يُستخدم داخل `callback_data` لأزرار اختيار الجودة —
     تيليجرام يفرض حداً صارماً بـ 64 بايت على `callback_data`، والرابط
     الكامل يتجاوزه بسهولة، بينما 8 أحرف hex + الفاصل + مفتاح الجودة
     يبقى دائماً تحت الحد (`dl|a1b2c3d4|v_1080` ≈ 16 بايت).
  2) نفس المفتاح يُستخدم كسجل كاش دائم — رابط قصير، قابل للفهرسة،
     ولا يحمل بيانات حساسة (لا يمكن استرجاع الرابط الأصلي من الهاش).

اختيار الـ backend — SQLite افتراضياً، Redis اختياري:
------------------------------------------------------
* **SQLite (افتراضي)**: صفر بنية تحتية خارجية — ملف واحد على القرص،
  مثالي لنسخة واحدة من البوت (وهو وضع التشغيل الشائع لهذا النوع من
  البوتات على Render/VPS واحد). يعمل تحت WAL mode لدعم قراءة/كتابة
  متزامنة بأمان من عدة coroutines بدون قفل الملف بالكامل.
* **Redis (اختياري، عبر ضبط REDIS_URL)**: يُفعَّل تلقائياً عند وجود
  المتغير — مناسب عند تشغيل عدة نسخ من البوت خلف نفس الـ webhook
  (توسّع أفقي) حيث يحتاج الكاش أن يكون مشتركاً بين العمليات، وليس
  ملف SQLite منفصل لكل نسخة. عند فشل الاتصال بـ Redis عند الإقلاع
  (مثلاً رابط خاطئ) يتراجع البوت تلقائياً لـ SQLite بدل التوقف الكامل.

كلا الـ backend يطبّقان نفس الواجهة (`CacheBackend`) — أي دعم مستقبلي
لـ PostgreSQL مثلاً يُضاف بنفس الطريقة بدون لمس أي كود يستخدم الكاش.
"""
import time
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("cache")


def short_hash(text: str, length: int = 8) -> str:
    """هاش قصير وحتمي (نفس النص → نفس الهاش دائماً) — يُستخدم كمعرف
    رابط في PENDING، في callback_data، ومفتاح الكاش."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


@dataclass
class CachedMedia:
    file_id:    str
    media_type: str   # "video" | "audio" | "document"
    title:      str
    created_at: float


# ══════════════════════════════════════════════
# واجهة الـ backend المشتركة
# ══════════════════════════════════════════════

class CacheBackend:
    async def init(self) -> None:
        raise NotImplementedError

    async def get(self, key: str) -> Optional[CachedMedia]:
        raise NotImplementedError

    async def set(self, key: str, media: CachedMedia) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


# ══════════════════════════════════════════════
# 🗄️ SQLite — الافتراضي، بدون بنية تحتية خارجية
# ══════════════════════════════════════════════

class SQLiteCache(CacheBackend):
    def __init__(self, path: str, ttl_seconds: Optional[int] = None):
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._db = None

    async def init(self) -> None:
        import aiosqlite
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS media_cache (
                key        TEXT PRIMARY KEY,
                file_id    TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title      TEXT,
                created_at REAL NOT NULL
            )"""
        )
        await self._db.commit()

    async def get(self, key: str) -> Optional[CachedMedia]:
        async with self._db.execute(
            "SELECT file_id, media_type, title, created_at FROM media_cache WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        file_id, media_type, title, created_at = row
        if self.ttl_seconds and (time.time() - created_at) > self.ttl_seconds:
            await self._db.execute("DELETE FROM media_cache WHERE key = ?", (key,))
            await self._db.commit()
            return None
        return CachedMedia(file_id=file_id, media_type=media_type, title=title or "", created_at=created_at)

    async def set(self, key: str, media: CachedMedia) -> None:
        await self._db.execute(
            """INSERT INTO media_cache (key, file_id, media_type, title, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   file_id=excluded.file_id, media_type=excluded.media_type,
                   title=excluded.title, created_at=excluded.created_at""",
            (key, media.file_id, media.media_type, media.title, media.created_at),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()


# ══════════════════════════════════════════════
# 🚀 Redis — اختياري، لتوسّع أفقي متعدد النسخ
# ══════════════════════════════════════════════

class RedisCache(CacheBackend):
    def __init__(self, url: str, ttl_seconds: Optional[int] = None):
        self.url = url
        self.ttl_seconds = ttl_seconds
        self._r = None

    async def init(self) -> None:
        import redis.asyncio as redis
        self._r = redis.from_url(self.url, decode_responses=True)
        await self._r.ping()  # يرمي استثناء فوراً إن كان الرابط/الخادم غير صالح

    async def get(self, key: str) -> Optional[CachedMedia]:
        import json
        raw = await self._r.get(f"mediacache:{key}")
        if not raw:
            return None
        d = json.loads(raw)
        return CachedMedia(**d)

    async def set(self, key: str, media: CachedMedia) -> None:
        import json
        payload = json.dumps({
            "file_id": media.file_id, "media_type": media.media_type,
            "title": media.title, "created_at": media.created_at,
        })
        await self._r.set(f"mediacache:{key}", payload, ex=self.ttl_seconds)

    async def close(self) -> None:
        if self._r:
            await self._r.aclose()


# ══════════════════════════════════════════════
# 🎛️ نقطة دخول واحدة يستخدمها main.py — تختار الـ backend المناسب
# ══════════════════════════════════════════════

_backend: Optional[CacheBackend] = None
_enabled: bool = True


async def init_cache(cfg) -> None:
    """يُستدعى مرة واحدة عند إقلاع البوت (app.before_server_start)."""
    global _backend, _enabled
    _enabled = bool(getattr(cfg, "CACHE_ENABLED", True))
    if not _enabled:
        logger.info("[cache] ⏸️ الكاش مُعطَّل عبر CACHE_ENABLED=false")
        return

    if cfg.REDIS_URL:
        try:
            backend = RedisCache(cfg.REDIS_URL, cfg.CACHE_TTL)
            await backend.init()
            _backend = backend
            logger.info("[cache] ✅ Redis backend نشط — مناسب للتشغيل متعدد النسخ")
            return
        except Exception as e:
            logger.warning(f"[cache] ⚠️ تعذّر الاتصال بـ Redis ({e}) — التراجع إلى SQLite")

    backend = SQLiteCache(cfg.CACHE_DB_PATH, cfg.CACHE_TTL)
    await backend.init()
    _backend = backend
    logger.info(f"[cache] ✅ SQLite backend نشط — {cfg.CACHE_DB_PATH}")


async def get_cached(url_hash: str, quality_key: str) -> Optional[CachedMedia]:
    """يرجع الوسائط المخزَّنة لهذا (الرابط، الجودة) أو None إن لم توجد/انتهت صلاحيتها."""
    if not _enabled or _backend is None:
        return None
    try:
        return await _backend.get(f"{url_hash}:{quality_key}")
    except Exception:
        logger.exception("[cache] فشل قراءة الكاش — سيُتابَع التحميل العادي")
        return None


async def set_cached(url_hash: str, quality_key: str, file_id: str, media_type: str, title: str) -> None:
    """يخزّن file_id بعد رفع ناجح — يُستدعى فقط عندما يبقى الملف جزءاً واحداً (غير مُقسَّم)."""
    if not _enabled or _backend is None:
        return
    try:
        media = CachedMedia(file_id=file_id, media_type=media_type, title=title, created_at=time.time())
        await _backend.set(f"{url_hash}:{quality_key}", media)
    except Exception:
        logger.exception("[cache] فشل تخزين النتيجة بالكاش — لن يؤثر على إرسال هذا الطلب")


async def close_cache() -> None:
    global _backend
    if _backend:
        await _backend.close()
        _backend = None
