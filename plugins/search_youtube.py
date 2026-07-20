# -*- coding: utf-8 -*-
"""
plugins/search_youtube.py
==========================
Search-plugin: يبحث عن أغاني/فيديوهات على YouTube بالاسم عبر yt-dlp
(extract_flat فقط — بدون تحميل فعلي، سريع).
"""
import asyncio, logging
from plugin_loader import SearchResult

logger = logging.getLogger("plugin.search_youtube")

DESCRIPTION     = "بحث بالاسم عبر YouTube"
SEARCH_PRIORITY = 10   # يُعرض أولاً غالباً

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"


def _fmt_duration(secs) -> str:
    if not secs:
        return ""
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def search(query: str):
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "http_headers": {"User-Agent": _UA},
    }

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch10:{query}", download=False)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=30)
    except Exception as e:
        logger.warning(f"[search] فشل بحث YouTube عن «{query}»: {e}")
        return []

    entries = (info or {}).get("entries") or []
    results = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
        if not url:
            continue
        results.append(SearchResult(
            title=e.get("title") or "بدون عنوان",
            url=url,
            source="YouTube",
            duration=_fmt_duration(e.get("duration")),
            uploader=e.get("uploader") or e.get("channel") or "",
        ))
    return results
