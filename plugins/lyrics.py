# -*- coding: utf-8 -*-
"""
plugins/lyrics.py
==================
Handler-plugin: يوفّر أمر /lyrics بطريقتين:
  1) /lyrics <عنوان الأغنية أو "الفنان - العنوان">  → بحث مباشر
  2) رد (reply) على رسالة صوت/فيديو بـ /lyrics       → تعرّف عبر Shazam
     (يُعيد استخدام plugins/shazam.py) ثم بحث عن الكلمات لنفس النتيجة.

المصدر: lyrics.ovh (مجاني، بدون مفتاح API).
"""
import logging
import aiohttp
from config import config
from plugin_loader import get_http_session

logger = logging.getLogger("plugin.lyrics")

DESCRIPTION = "عرض كلمات الأغاني عبر /lyrics (نصاً أو رداً على مقطع صوتي/فيديو)"

_LYRICS_API = config.LYRICS_API
_TG_MSG_LIMIT = 4096


def _is_lyrics_command(msg: dict) -> bool:
    from telegram_api import is_command, command_name
    return is_command(msg) and command_name(msg) == "lyrics"


async def _fetch_lyrics(artist: str, title: str) -> str:
    sess = await get_http_session()
    async with sess.get(
        f"{_LYRICS_API}/{artist}/{title}",
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status != 200:
            raise Exception("لم يُعثر على كلمات لهذه الأغنية")
        data = await r.json(content_type=None)
    lyrics = (data or {}).get("lyrics", "").strip()
    if not lyrics:
        raise Exception("لم يُعثر على كلمات لهذه الأغنية")
    return lyrics


def _split_query(query: str):
    """يحاول فصل 'فنان - عنوان'؛ إن لم يوجد فاصل يُستخدم النص كله كعنوان."""
    if " - " in query:
        artist, title = query.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", query.strip()


async def _reply_with_lyrics(bot, chat_id: int, status_message_id: int, artist: str, title: str):
    try:
        lyrics = await _fetch_lyrics(artist or "", title)
    except Exception as e:
        await bot.edit_message_text(chat_id, status_message_id, f"❌ {str(e)[:200]}")
        return

    header = f"🎵 *{title}*" + (f" — {artist}" if artist else "") + "\n\n"
    full   = header + lyrics
    await bot.edit_message_text(chat_id, status_message_id, full[:_TG_MSG_LIMIT], parse_mode="Markdown")
    # إرسال الباقي كرسائل إضافية إن تجاوزت حد تيليجرام
    rest = full[_TG_MSG_LIMIT:]
    while rest:
        chunk, rest = rest[:_TG_MSG_LIMIT], rest[_TG_MSG_LIMIT:]
        await bot.send_message(chat_id, chunk)


async def handle_lyrics_command(msg: dict, bot):
    from telegram_api import command_args
    chat_id = msg["chat"]["id"]
    query   = command_args(msg)
    reply   = msg.get("reply_to_message")

    status = await bot.send_message(chat_id, "🔍 جاري البحث عن الكلمات...")

    if reply and not query:
        # الوضع الثاني: رد على مقطع صوت/فيديو → تعرّف عبر Shazam أولاً
        import plugins.shazam as shazam_mod
        try:
            track = await shazam_mod.identify_from_message(reply, bot)
        except Exception as e:
            await bot.edit_message_text(chat_id, status["message_id"], f"❌ تعذّر التعرف على المقطع:\n{str(e)[:200]}")
            return
        await _reply_with_lyrics(bot, chat_id, status["message_id"], track["artist"], track["title"])
        return

    if not query:
        await bot.edit_message_text(
            chat_id, status["message_id"],
            "❌ استخدم: /lyrics <اسم الأغنية> أو رد على مقطع صوتي/فيديو بالأمر /lyrics"
        )
        return

    artist, title = _split_query(query)
    await _reply_with_lyrics(bot, chat_id, status["message_id"], artist, title)


def register_plugin():
    return {"filter": _is_lyrics_command, "callback": handle_lyrics_command}
