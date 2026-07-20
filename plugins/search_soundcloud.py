# -*- coding: utf-8 -*-
"""
plugins/search_soundcloud.py
=============================
Search-plugin: يبحث عن مقاطع صوتية على SoundCloud بالاسم عبر API العام.
مستقل عن plugins/soundcloud.py (لا اعتمادية متبادلة) لتفادي مشاكل ترتيب
تحميل الـ plugins، لكنه يستخدم نفس الجلسة المشتركة من plugin_loader.
"""
import logging
import aiohttp
from plugin_loader import SearchResult, get_http_session, get_soundcloud_client_id

logger = logging.getLogger("plugin.search_soundcloud")

DESCRIPTION     = "بحث بالاسم عبر SoundCloud"
SEARCH_PRIORITY = 20   # يُعرض بعد YouTube

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
_H  = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

# ⚡ client_id مشترك مع plugins/soundcloud.py عبر plugin_loader (كاش واحد
#   بدل طلبين منفصلين لصفحة SoundCloud لاستخراج نفس القيمة)
async def _get_client_id(sess) -> str:
    return await get_soundcloud_client_id(sess, _H)


def _fmt_duration(ms) -> str:
    if not ms:
        return ""
    secs = int(ms / 1000)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def search(query: str):
    try:
        sess = await get_http_session()
        cid  = await _get_client_id(sess)
        async with sess.get(
            "https://api-v2.soundcloud.com/search/tracks",
            params={"q": query, "client_id": cid, "limit": 10},
            headers=_H, timeout=aiohttp.ClientTimeout(total=12)
        ) as r:
            data = await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"[search] فشل بحث SoundCloud عن «{query}»: {e}")
        return []

    items = (data or {}).get("collection") or []
    results = []
    for t in items[:10]:
        url = t.get("permalink_url")
        if not url:
            continue
        results.append(SearchResult(
            title=t.get("title") or "بدون عنوان",
            url=url,
            source="SoundCloud",
            duration=_fmt_duration(t.get("duration")),
            uploader=(t.get("user") or {}).get("username", ""),
        ))
    return results
