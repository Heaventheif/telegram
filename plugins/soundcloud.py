# -*- coding: utf-8 -*-
"""plugins/soundcloud.py — ساوندكلاود عبر API مباشر"""
import logging
import aiohttp
from config import config
from plugin_loader import (
    ProbeResult, DownloadResult, QualityOption, get_http_session,
    get_soundcloud_client_id, stream_to_file,
)

logger = logging.getLogger("plugin.soundcloud")

DESCRIPTION = "ساوندكلاود — streaming مباشر"
DOMAINS     = ["soundcloud.com"]
PRIORITY    = 10

UPLOAD_LIMIT = config.UPLOAD_LIMIT

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
_H  = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

# ⚡ client_id مشترك مع search_soundcloud.py عبر plugin_loader (بدل نسخة
#   منفصلة لكل ملف تطلب صفحة SoundCloud بشكل مكرر)
async def _get_client_id(sess) -> str:
    return await get_soundcloud_client_id(sess, _H)

async def probe(url: str) -> ProbeResult:
    # نحاول نجلب عنوان المقطع
    title = "مقطع SoundCloud"
    try:
        sess = await get_http_session()
        cid  = await _get_client_id(sess)
        async with sess.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": url, "client_id": cid},
            headers=_H, timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json(content_type=None)
        title = (data or {}).get("title") or title
    except Exception as e:
        logger.warning(f"[probe] فشل جلب العنوان: {e}")

    return ProbeResult(
        title=title,
        options=[
            QualityOption(kind="audio", label="🎵 تحميل MP3 (كامل)", key="a_full", size_hint=0),
            QualityOption(kind="audio", label="🎵 معاينة 30 ثانية",  key="a_snip", size_hint=0),
        ],
        extra={"url": url},
    )

async def download(url: str, choice: dict) -> DownloadResult:
    key      = choice["key"]
    want_snip = key == "a_snip"

    sess = await get_http_session()
    cid  = await _get_client_id(sess)

    # resolve
    async with sess.get(
        "https://api-v2.soundcloud.com/resolve",
        params={"url": url, "client_id": cid},
        headers=_H, timeout=aiohttp.ClientTimeout(total=10)
    ) as r:
        track = await r.json(content_type=None)

    title         = track.get("title", "بدون عنوان")
    transcodings  = (track.get("media") or {}).get("transcodings") or []
    if not transcodings:
        raise Exception("لا يوجد بث متاح لهذا المقطع")

    def _pick():
        prot_pref = "progressive"
        candidates = [t for t in transcodings
                      if (want_snip == bool(t.get("snipped"))) and t.get("format",{}).get("protocol") == prot_pref]
        if not candidates:
            candidates = [t for t in transcodings if want_snip == bool(t.get("snipped"))]
        if not candidates:
            candidates = [t for t in transcodings if t.get("format",{}).get("protocol") == prot_pref]
        return (candidates or transcodings)[0]

    chosen = _pick()
    async with sess.get(
        chosen["url"],
        params={"client_id": cid, "track_authorization": track.get("track_authorization","")},
        headers=_H, timeout=aiohttp.ClientTimeout(total=15)
    ) as r:
        stream_data = await r.json(content_type=None)

    stream_url = (stream_data or {}).get("url")
    if not stream_url:
        raise Exception("فشل استخراج رابط البث")

    # ⚡ streaming غير-blocking عبر aiofiles + إيقاف مبكر إذا تجاوز حد الرفع
    path = await stream_to_file(sess, stream_url, ".mp3", headers=_H,
                                 timeout_total=60, max_size=UPLOAD_LIMIT)
    return DownloadResult(file_path=path, title=title, is_audio=True)
