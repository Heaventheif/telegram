# -*- coding: utf-8 -*-
"""
telegram_api.py — عميل خفيف لـ Telegram Bot API عبر aiohttp مباشرة
====================================================================
يستبدل python-telegram-bot بالكامل. لا يحمل Job Queue ولا Persistence
ولا Rate-Limiter داخلي — فقط استدعاءات HTTP مباشرة لواجهة تيليجرام،
باستخدام نفس جلسة aiohttp المشتركة في plugin_loader (بدون اتصالات
مكررة). هذا يزيل الحمل الثابت لكائن Application وكل ما يدور معه.

الاستخدام من الـ plugins (خصوصاً handler-plugins مثل shazam.py) مبني
على قواميس (dict) خام لرسائل تيليجرام بدل كائنات telegram.Update، مع
مجموعة دوال فلترة بسيطة في filters_simple أدناه.
"""
import os
import logging
import aiohttp

logger = logging.getLogger("telegram_api")


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, token: str, session_getter):
        self.token = token
        self._get_session = session_getter
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    async def _call(self, method: str, **params):
        sess = await self._get_session()
        payload = {k: v for k, v in params.items() if v is not None}
        async with sess.post(f"{self.api_base}/{method}", json=payload) as r:
            data = await r.json()
        if not data.get("ok"):
            raise TelegramError(f"[{method}] {data.get('description', data)}")
        return data["result"]

    # ── رسائل نصية ──
    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        return await self._call(
            "sendMessage", chat_id=chat_id, text=text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        try:
            return await self._call(
                "editMessageText", chat_id=chat_id, message_id=message_id,
                text=text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
        except TelegramError as e:
            # تجاهل الخطأ الشائع "message is not modified"
            if "not modified" in str(e):
                return None
            raise

    async def delete_message(self, chat_id, message_id):
        try:
            return await self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError:
            logger.warning(f"[delete_message] فشل حذف الرسالة {message_id} في {chat_id}")
            return None

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        try:
            return await self._call(
                "answerCallbackQuery", callback_query_id=callback_query_id,
                text=text, show_alert=show_alert,
            )
        except TelegramError:
            logger.warning("[answer_callback_query] فشل — على الأرجح انتهت صلاحية الاستعلام")
            return None

    async def send_photo(self, chat_id, photo, caption=None):
        return await self._call("sendPhoto", chat_id=chat_id, photo=photo, caption=caption)

    # ── ملفات (multipart) ──
    async def _send_file(self, method: str, field: str, chat_id, file_path: str,
                          filename: str = None, caption: str = None, extra_fields: dict = None):
        sess = await self._get_session()
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        if caption:
            data.add_field("caption", caption)
        for k, v in (extra_fields or {}).items():
            if v is not None:
                data.add_field(k, str(v))
        fname = filename or os.path.basename(file_path)
        with open(file_path, "rb") as f:
            data.add_field(field, f, filename=fname)
            async with sess.post(
                f"{self.api_base}/{method}", data=data,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                res = await r.json()
        if not res.get("ok"):
            raise TelegramError(f"[{method}] {res.get('description', res)}")
        return res["result"]

    async def send_document(self, chat_id, file_path, filename=None, caption=None):
        return await self._send_file("sendDocument", "document", chat_id, file_path, filename, caption)

    # ── إعادة إرسال فورية عبر file_id مخزَّن بالكاش (بدون رفع، JSON بسيط) ──
    # تيليجرام يقبل file_id سبق ورُفع كقيمة عادية لحقل الوسائط في
    # sendVideo/sendAudio/sendDocument — أسرع بكثير من _send_file لأنه
    # لا يعيد رفع أي بايتات، فقط يطلب من تيليجرام إعادة استخدام الملف
    # الموجود مسبقاً على خوادمه.
    async def send_cached_video(self, chat_id, file_id, caption=None):
        return await self._call("sendVideo", chat_id=chat_id, video=file_id, caption=caption)

    async def send_cached_audio(self, chat_id, file_id, caption=None, title=None):
        return await self._call("sendAudio", chat_id=chat_id, audio=file_id, caption=caption, title=title)

    async def send_cached_document(self, chat_id, file_id, caption=None):
        return await self._call("sendDocument", chat_id=chat_id, document=file_id, caption=caption)

    async def send_audio(self, chat_id, file_path, title=None, caption=None):
        return await self._send_file(
            "sendAudio", "audio", chat_id, file_path,
            caption=caption, extra_fields={"title": title},
        )

    async def send_video(self, chat_id, file_path, caption=None):
        return await self._send_file("sendVideo", "video", chat_id, file_path, caption=caption)

    # ── تنزيل ملفات مُرسَلة من المستخدم (لـ shazam/lyrics) ──
    async def download_file(self, file_id: str, dest_path: str):
        info = await self._call("getFile", file_id=file_id)
        tg_path = info["file_path"]
        sess = await self._get_session()
        async with sess.get(f"{self.file_base}/{tg_path}") as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)
        return dest_path

    # ── إقلاع/إيقاف ──
    async def set_webhook(self, url):
        return await self._call("setWebhook", url=url)


# ══════════════════════════════════════════════
# فلاتر بسيطة على قواميس رسائل تيليجرام الخام
# ══════════════════════════════════════════════

def is_command(msg: dict) -> bool:
    return bool(msg.get("text", "").startswith("/"))

def is_plain_text(msg: dict) -> bool:
    return "text" in msg and not is_command(msg)

def is_voice(msg: dict) -> bool:
    return "voice" in msg

def is_audio(msg: dict) -> bool:
    return "audio" in msg

def is_video(msg: dict) -> bool:
    return "video" in msg

def is_video_note(msg: dict) -> bool:
    return "video_note" in msg

def is_recognizable_media(msg: dict) -> bool:
    return is_voice(msg) or is_audio(msg) or is_video(msg) or is_video_note(msg)

def command_name(msg: dict) -> str:
    """يرجع اسم الأمر بدون '/' وبدون @botusername، مثلاً '/lyrics@Bot' -> 'lyrics'."""
    text = msg.get("text", "")
    first = text.split()[0] if text.split() else ""
    return first[1:].split("@")[0].lower()

def command_args(msg: dict) -> str:
    """يرجع بقية النص بعد اسم الأمر."""
    text = msg.get("text", "")
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
