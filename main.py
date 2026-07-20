# -*- coding: utf-8 -*-
"""
main.py — ثابت نهائياً، لا يُعدَّل أبداً
==========================================
كل ميزة جديدة تُضاف بملف واحد في plugins/

هذا الملف لا يعتمد على python-telegram-bot — فقط استدعاءات Bot API خام
عبر telegram_api.Bot (نفس جلسة aiohttp المشتركة)، لإزالة الحمل الثابت
لكائن Application وكل ما يدور معه (job queue, persistence, rate limiter).

⚡ كاش الوسائط (media_cache): كل رابط + جودة مختارة يُعرَّف بهاش قصير
حتمي (8 أحرف — راجع cache.py) يُستخدم كتوكن PENDING وكمفتاح كاش في آن
واحد. أول تحميل لرابط+جودة معيّنة يُخزَّن file_id الناتج؛ أي طلب لاحق
لنفس الرابط بنفس الجودة يُعاد إرساله فوراً من تيليجرام دون تحميل/رفع.
"""
import os, sys, re, time, asyncio, logging

from sanic import Sanic, response as sanic_response

from config import config
import cache
import plugin_loader
from plugin_loader import (
    load_all_plugins, run_pending_setups, find_plugin, get_registry,
    get_plugins, get_download_semaphore, get_search_providers,
    get_extra_handlers, get_http_session, close_http_session,
    split_media, QualityOption,
)
from telegram_api import Bot, is_command, is_plain_text, command_name, command_args

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("main")

logging.getLogger("aiohttp").setLevel(logging.WARNING)

# ══════════════════════════════════════════════
# ⚙️ إعدادات — كل شيء مركزي عبر config.py الآن
# ══════════════════════════════════════════════
config.validate()  # يوقف العملية فوراً برسالة واضحة إن نقصت TELEGRAM_TOKEN/SERVER_URL

UPLOAD_LIMIT = config.UPLOAD_LIMIT
URL_RE       = re.compile(r"https?://\S+")

app = Sanic("MediaBot")
bot = Bot(config.TELEGRAM_TOKEN, get_http_session)

# ══════════════════════════════════════════════
# 🗄️ PENDING — تخزين مؤقت للطلبات (المفتاح = هاش قصير حتمي للرابط)
# ══════════════════════════════════════════════
PENDING     = {}
PENDING_TTL = config.PENDING_TTL_MIN * 60

def _cleanup():
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["ts"] > PENDING_TTL]:
        PENDING.pop(k, None)

# ══════════════════════════════════════════════
# 🔎 SEARCH_PENDING — تخزين مؤقت لنتائج البحث النصي
# ══════════════════════════════════════════════
SEARCH_PENDING     = {}
SEARCH_PENDING_TTL = config.SEARCH_PENDING_TTL_MIN * 60

def _cleanup_search():
    now = time.time()
    for k in [k for k, v in SEARCH_PENDING.items() if now - v["ts"] > SEARCH_PENDING_TTL]:
        SEARCH_PENDING.pop(k, None)

# ══════════════════════════════════════════════
# 🔀 URL_MODE_PENDING — بانتظار اختيار المستخدم "تنزيل كامل" أو "تنزيل جزء
# محدد بالوقت" لرابط جديد أُرسل للتو (قبل عرض قائمة الجودات)
# ══════════════════════════════════════════════
URL_MODE_PENDING     = {}
URL_MODE_PENDING_TTL = config.PENDING_TTL_MIN * 60

def _cleanup_url_mode():
    now = time.time()
    for k in [k for k, v in URL_MODE_PENDING.items() if now - v["ts"] > URL_MODE_PENDING_TTL]:
        URL_MODE_PENDING.pop(k, None)

# chat_id -> {"url": str, "status_message_id": int, "ts": float}
# بانتظار رد نصي بصيغة "البداية-النهاية" بعد اختيار "تنزيل جزء محدد"
URL_CLIP_AWAIT = {}

# ══════════════════════════════════════════════
# ⌨️ بناء لوحة الأزرار من QualityOption list
# ══════════════════════════════════════════════
def _build_keyboard(token: str, options: list) -> dict:
    """يبني InlineKeyboardMarkup (كقاموس خام) من قائمة QualityOption.
    يضع أزرار الفيديو أولاً (3 لكل صف) ثم أزرار الصوت (3 لكل صف).
    التوكن هاش قصير (8 أحرف) → callback_data مثل 'dl|a1b2c3d4|v_1080'
    يبقى دائماً تحت حد تيليجرام الصارم البالغ 64 بايت."""
    videos = [o for o in options if o.kind == "video"]
    audios = [o for o in options if o.kind == "audio"]
    rows   = []

    def _chunk(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    for chunk in _chunk(videos, 3):
        rows.append([{"text": o.label, "callback_data": f"dl|{token}|{o.key}"} for o in chunk])
    for chunk in _chunk(audios, 3):
        rows.append([{"text": o.label, "callback_data": f"dl|{token}|{o.key}"} for o in chunk])

    return {"inline_keyboard": rows}

# ══════════════════════════════════════════════
# 🔍 فحص رابط وعرض خيارات الجودة (مشتركة بين مسار الرابط ومسار اختيار نتيجة بحث)
# ══════════════════════════════════════════════
async def _probe_and_present(url: str, chat_id: int, message_id: int):
    plugin = find_plugin(url)
    if not plugin:
        await bot.edit_message_text(
            chat_id, message_id,
            "❌ هذا الموقع غير مدعوم حالياً.\nأرسل رابطاً من يوتيوب، تيك توك، انستغرام، فيسبوك، تويتر/X، ساوندكلاود، Bilibili، Twitch، Reddit..."
        )
        return

    await bot.edit_message_text(chat_id, message_id, f"🔍 جاري فحص الرابط عبر [{plugin['name']}]...")

    try:
        result = await asyncio.wait_for(plugin["probe"](url), timeout=45)
    except asyncio.TimeoutError:
        logger.warning(f"[{plugin['name']}] probe تجاوز المهلة الزمنية | url={url} | chat={chat_id}")
        await bot.edit_message_text(chat_id, message_id, "❌ استغرق فحص الرابط وقتاً طويلاً جداً. حاول مجدداً لاحقاً.")
        return
    except Exception as e:
        logger.exception(f"[{plugin['name']}] probe فشل | url={url} | chat={chat_id}")
        await bot.edit_message_text(chat_id, message_id, f"❌ تعذّر فحص الرابط:\n{str(e)[:300]}")
        return

    if not result or not result.options:
        await bot.edit_message_text(chat_id, message_id, "❌ لم تُوجد جودات متاحة لهذا الرابط.")
        return

    _cleanup()
    # ⚡ هاش قصير حتمي بدل uuid عشوائي — نفس الرابط ينتج دائماً نفس
    # التوكن، فيُستخدم مباشرة كمفتاح كاش file_id عند اختيار الجودة.
    token = cache.short_hash(url, config.CACHE_HASH_LEN)
    PENDING[token] = {
        "url":     url,
        "plugin":  plugin["name"],
        "title":   result.title,
        "options": {o.key: o for o in result.options},
        "extra":   result.extra,
        "ts":      time.time(),
    }

    kb = _build_keyboard(token, result.options)
    await bot.edit_message_text(
        chat_id, message_id,
        f"🎬 *{result.title}*\n\nاختر جودة الفيديو 🎥 أو صيغة الصوت 🎵:",
        reply_markup=kb, parse_mode="Markdown",
    )

# ══════════════════════════════════════════════
# 🔎 بحث نصي متعدد المنصات → قائمة نتائج → اختيار → probe عادي
# ══════════════════════════════════════════════
_SOURCE_EMOJI = {"YouTube": "▶️", "SoundCloud": "🟠"}

async def handle_search_query(query_text: str, chat_id: int):
    providers = get_search_providers()
    if not providers:
        return  # لا توجد مزودات بحث مُحمَّلة — تجاهل بصمت

    status = await bot.send_message(chat_id, f"🔍 جاري البحث عن «{query_text}»...")

    all_results = []
    for p in providers:
        try:
            res = await p["search"](query_text)
            all_results.extend(res or [])
        except Exception:
            logger.exception(f"[{p['name']}] search فشل | query={query_text}")

    if not all_results:
        await bot.edit_message_text(chat_id, status["message_id"], "❌ لم أجد نتائج مطابقة. جرّب صياغة مختلفة، أو أرسل رابطاً مباشرة.")
        return

    results = all_results[:10]

    _cleanup_search()
    token = cache.short_hash(f"{query_text}|{time.time()}", config.CACHE_HASH_LEN)
    SEARCH_PENDING[token] = {"results": results, "query": query_text, "ts": time.time()}

    # رسالة واحدة تجمع كل النتائج — كل نتيجة سطر/زر مستقل ضمن نفس الرسالة
    # (بدل رسالة منفصلة لكل نتيجة بزر "تحميل" أسفلها)
    rows = []
    for i, r in enumerate(results):
        emoji = _SOURCE_EMOJI.get(r.source, "🎵")
        dur   = f" · {r.duration}" if r.duration else ""
        label = f"{i+1}. {emoji} {r.title}{dur}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([{"text": label, "callback_data": f"srch|{token}|{i}"}])

    await bot.edit_message_text(
        chat_id, status["message_id"],
        f"🔍 نتائج البحث عن «{query_text}»:",
        reply_markup={"inline_keyboard": rows},
    )


async def handle_search_choice(callback_query: dict):
    msg     = callback_query["message"]
    chat_id = msg["chat"]["id"]
    data    = callback_query.get("data") or ""

    try:
        _, token, idx_s = data.split("|", 2)
        idx = int(idx_s)
    except ValueError:
        await bot.answer_callback_query(callback_query["id"], "⚠️ طلب غير صالح.", show_alert=True)
        return

    _cleanup_search()
    task = SEARCH_PENDING.get(token)
    if not task:
        await bot.answer_callback_query(callback_query["id"], "⌛ انتهت صلاحية نتائج البحث، أعد البحث.", show_alert=True)
        return

    results = task["results"]
    if idx < 0 or idx >= len(results):
        await bot.answer_callback_query(callback_query["id"], "⚠️ خيار غير موجود.", show_alert=True)
        return

    await bot.answer_callback_query(callback_query["id"])
    chosen = results[idx]
    SEARCH_PENDING.pop(token, None)

    await bot.edit_message_text(chat_id, msg["message_id"], f"🔍 جاري فحص: {chosen.title} ({chosen.source})...")
    await _probe_and_present(chosen.url, chat_id, msg["message_id"])

# ══════════════════════════════════════════════
# 📨 استقبال الرسائل النصية → رابط مباشر أو بحث أو أمر
# ══════════════════════════════════════════════
async def handle_message(msg: dict):
    text = (msg.get("text") or "").strip()
    if not text:
        return
    chat_id = msg["chat"]["id"]

    # ✂️ إن كان بانتظار رد بوقت الجزء المطلوب (بعد اختيار "تنزيل جزء محدد")
    pending_clip = URL_CLIP_AWAIT.get(chat_id)
    if pending_clip and not URL_RE.search(text):
        URL_CLIP_AWAIT.pop(chat_id, None)
        await _handle_url_clip_time(chat_id, text, pending_clip)
        return
    if pending_clip:
        # المستخدم أرسل رابطاً جديداً بدل الوقت — نُلغي الانتظار ونكمل برابطه الجديد
        URL_CLIP_AWAIT.pop(chat_id, None)

    m = URL_RE.search(text)
    if m:
        url = m.group(0)
        _cleanup_url_mode()
        token = cache.short_hash(url, config.CACHE_HASH_LEN)
        URL_MODE_PENDING[token] = {"url": url, "ts": time.time()}
        kb = {
            "inline_keyboard": [[
                {"text": "📥 تنزيل كامل",      "callback_data": f"mode|{token}|full"},
                {"text": "✂️ تنزيل جزء محدد", "callback_data": f"mode|{token}|part"},
            ]]
        }
        await bot.send_message(chat_id, "📥 كيف تريد تنزيل هذا الرابط؟", reply_markup=kb)
        return

    # نص بدون رابط → اعتباره طلب بحث (بحد أدنى وأقصى معقول للطول)
    if 2 <= len(text) <= 100:
        await handle_search_query(text, chat_id)


# ══════════════════════════════════════════════
# 🔀 اختيار المستخدم: تنزيل كامل مباشرة، أو تحديد وقت جزء أولاً
# ══════════════════════════════════════════════
async def handle_url_mode_choice(callback_query: dict):
    msg_container = callback_query["message"]
    chat_id       = msg_container["chat"]["id"]
    status_id     = msg_container["message_id"]
    data          = callback_query.get("data") or ""

    try:
        _, token, mode = data.split("|", 2)
    except ValueError:
        await bot.answer_callback_query(callback_query["id"], "⚠️ طلب غير صالح.", show_alert=True)
        return

    _cleanup_url_mode()
    entry = URL_MODE_PENDING.get(token)
    if not entry:
        await bot.answer_callback_query(callback_query["id"], "⌛ انتهت الصلاحية، أعد إرسال الرابط.", show_alert=True)
        return

    await bot.answer_callback_query(callback_query["id"])
    url = entry["url"]
    URL_MODE_PENDING.pop(token, None)

    if mode == "full":
        await bot.edit_message_text(chat_id, status_id, "🔍 جاري التحقق من الرابط...")
        await _probe_and_present(url, chat_id, status_id)
        return

    if mode == "part":
        await bot.edit_message_text(
            chat_id, status_id,
            "⏱️ أرسل وقت الجزء المطلوب بصيغة (البداية-النهاية) mm:ss\n"
            "مثال: `0:30-1:15`",
            parse_mode="Markdown",
        )
        URL_CLIP_AWAIT[chat_id] = {"url": url, "status_message_id": status_id, "ts": time.time()}
        return

    await bot.answer_callback_query(callback_query["id"], "⚠️ خيار غير معروف.", show_alert=True)


async def _handle_url_clip_time(chat_id: int, text: str, pending: dict):
    import plugins.media_tools as mtool_mod  # لإعادة استخدام محلّل صيغة الوقت الموحّد
    status_id = pending["status_message_id"]
    url       = pending["url"]

    try:
        start, end = mtool_mod.parse_time_range(text)
    except ValueError as e:
        await bot.send_message(chat_id, f"❌ {e}")
        URL_CLIP_AWAIT[chat_id] = pending  # أعد الانتظار ليحاول المستخدم مجدداً
        return

    await bot.edit_message_text(chat_id, status_id, "🔍 جاري التحقق من الرابط...")
    await _probe_and_present(url, chat_id, status_id)

    # نُرفق وقت المقطع بطلب PENDING الذي أنشأه _probe_and_present للتو — يُقرأ
    # لاحقاً في handle_choice بعد التحميل لقص الجزء المطلوب قبل الإرسال
    token = cache.short_hash(url, config.CACHE_HASH_LEN)
    if token in PENDING:
        PENDING[token]["clip"] = {"start": start, "end": end}

# ══════════════════════════════════════════════
# ⚡ إرسال فوري من الكاش — بدون تحميل/رفع
# ══════════════════════════════════════════════
async def _send_cached(chat_id: int, cached: "cache.CachedMedia", status_message_id: int):
    """يعيد إرسال file_id مخزَّن مسبقاً. يرمي استثناء إن رفضه تيليجرام
    (مثلاً file_id غير صالح/منتهي) ليتراجع المستدعي لتحميل عادي."""
    if cached.media_type == "video":
        await bot.send_cached_video(chat_id, cached.file_id, caption=None)
    elif cached.media_type == "audio":
        await bot.send_cached_audio(chat_id, cached.file_id, caption=None, title=cached.title)
    else:
        await bot.send_cached_document(chat_id, cached.file_id, caption=None)
    await bot.delete_message(chat_id, status_message_id)

# ══════════════════════════════════════════════
# ⬇️ اختيار المستخدم → تحميل، تقسيم إذا لزم، وإرسال
# ══════════════════════════════════════════════
async def _send_result(chat_id: int, dl, status_message_id: int, task: dict, cache_key: tuple = None):
    """يرسل نتيجة التحميل — يقسّمها تلقائياً عبر ffmpeg إذا تجاوزت حد تيليجرام.
    عندما يبقى الملف جزءاً واحداً (غير مُقسَّم) ويُمرَّر cache_key=(url_hash,
    quality_key)، يُخزَّن file_id الناتج بالكاش لتسريع الطلبات المكرَّرة لاحقاً.
    الملفات المُقسَّمة لا تُخزَّن (عدة file_id لا تناسب مفتاح كاش واحد)."""
    fsize = os.path.getsize(dl.file_path)

    parts = [dl.file_path]
    if fsize > UPLOAD_LIMIT:
        if dl.is_document:
            # الملفات كمستندات لا تُقسَّم — نرفضها كما كان
            _cleanup_file(dl.file_path)
            await bot.edit_message_text(
                chat_id, status_message_id,
                f"❌ حجم الملف ({fsize/1024/1024:.1f}MB) يتجاوز حد تيليجرام (50MB)."
            )
            return
        await bot.edit_message_text(
            chat_id, status_message_id,
            f"✂️ الملف ({fsize/1024/1024:.1f}MB) يتجاوز حد تيليجرام — جاري تقسيمه إلى أجزاء..."
        )
        try:
            parts = await split_media(dl.file_path, max_size=UPLOAD_LIMIT, is_audio=dl.is_audio)
        except Exception as e:
            logger.exception(f"[split] فشل تقسيم الملف | {dl.file_path}")
            _cleanup_file(dl.file_path)
            await bot.edit_message_text(chat_id, status_message_id, f"❌ فشل تقسيم الملف الكبير:\n{str(e)[:200]}")
            return
        if dl.file_path not in parts and os.path.exists(dl.file_path):
            _cleanup_file(dl.file_path)  # الملف الأصلي لم يعد يُستخدم بعد التقسيم

    total = len(parts)
    await bot.edit_message_text(
        chat_id, status_message_id,
        f"📤 جاري رفع {'الملف' if total == 1 else f'{total} أجزاء'} إلى تيليجرام..."
    )

    try:
        sent = None
        for i, part_path in enumerate(parts, 1):
            if dl.is_document:
                fname = dl.title if dl.title.lower().endswith(".zip") else os.path.basename(part_path)
                sent = await bot.send_document(chat_id, part_path, filename=fname, caption=None)
            elif dl.is_audio:
                sent = await bot.send_audio(chat_id, part_path, title=dl.title, caption=None)
            else:
                sent = await bot.send_video(chat_id, part_path, caption=None)
        await bot.delete_message(chat_id, status_message_id)

        # 💾 تخزين file_id بالكاش — فقط لملف واحد غير مُقسَّم
        if cache_key and total == 1 and sent:
            url_hash, quality_key = cache_key
            media_type = "document" if dl.is_document else ("audio" if dl.is_audio else "video")
            file_id = (sent.get(media_type) or {}).get("file_id")
            if file_id:
                await cache.set_cached(url_hash, quality_key, file_id, media_type, dl.title)
    except Exception as e:
        logger.exception(f"[send] فشل رفع الملف | plugin={task['plugin']} | chat={chat_id}")
        try:
            await bot.edit_message_text(chat_id, status_message_id, f"❌ فشل رفع الملف إلى تيليجرام:\n{str(e)[:200]}")
        except Exception:
            logger.exception("[send] فشل حتى تعديل رسالة الخطأ")
    finally:
        for p in parts:
            _cleanup_file(p)


async def handle_choice(callback_query: dict):
    msg     = callback_query["message"]
    chat_id = msg["chat"]["id"]
    data    = callback_query.get("data") or ""

    try:
        _, token, key = data.split("|", 2)
    except ValueError:
        await bot.answer_callback_query(callback_query["id"], "⚠️ طلب غير صالح.", show_alert=True)
        return

    _cleanup()
    task = PENDING.get(token)
    if not task:
        await bot.answer_callback_query(callback_query["id"], "⌛ انتهت صلاحية الطلب، أعد إرسال الرابط.", show_alert=True)
        return

    option = task["options"].get(key)
    if not option:
        await bot.answer_callback_query(callback_query["id"], "⚠️ خيار غير موجود.", show_alert=True)
        return

    await bot.answer_callback_query(callback_query["id"])

    # ⚡ فحص الكاش أولاً — نفس الرابط + نفس الجودة سبق ورُفع؟ إعادة إرسال فورية
    cached = await cache.get_cached(token, key)
    if cached:
        await bot.edit_message_text(
            chat_id, msg["message_id"],
            f"⚡ «{task['title']}» موجود في الكاش — جاري الإرسال الفوري..."
        )
        try:
            await _send_cached(chat_id, cached, msg["message_id"])
            PENDING.pop(token, None)
            return
        except Exception:
            logger.warning(f"[cache] file_id مخزَّن لم يعد صالحاً (token={token}, key={key}) — تحميل عادي بدلاً منه")
            # نتابع لمسار التحميل العادي بدل فشل الطلب بالكامل

    await bot.edit_message_text(chat_id, msg["message_id"], f"⏳ جاري تحميل «{task['title']}» — {option.label} ...")

    plugin_entry = next((p for p in get_plugins() if p["name"] == task["plugin"]), None)
    if not plugin_entry:
        await bot.edit_message_text(chat_id, msg["message_id"], "❌ الـ plugin الأصلي لم يُعثر عليه، أعد إرسال الرابط.")
        return

    t_dl_start = time.time()
    try:
        # 🚦 حد أقصى للتحميلات المتزامنة — يمنع ذروة استهلاك رام/قرص
        async with get_download_semaphore():
            dl = await plugin_entry["download"](
                url=task["url"],
                choice={"key": key, "option": option, "extra": task["extra"]},
            )
    except Exception as e:
        logger.exception(f"[{task['plugin']}] download فشل | url={task['url']} | chat={chat_id}")
        await bot.edit_message_text(chat_id, msg["message_id"], f"❌ فشل التحميل:\n{str(e)[:300]}")
        return
    logger.info(f"[{task['plugin']}] ⬇️ تحميل مكتمل في {time.time()-t_dl_start:.1f}s | {dl.file_path}")

    cache_key = (token, key)
    clip = task.get("clip")
    if clip and not dl.is_document:
        # ⚠️ نصل هنا فقط بعد اكتمال dl.file_path كتنزيل كامل ناجح تماماً (السطر
        # أعلاه plugin_entry["download"] إما رجع بنجاح بالملف الكامل أو رمى
        # استثناء وأوقف الدالة قبل الوصول لهذا الكود) — القص يتم دائماً على
        # نسخة كاملة محلياً عبر ffmpeg، لا على تدفق جزئي.
        import plugins.media_tools as mtool_mod
        await bot.edit_message_text(
            chat_id, msg["message_id"],
            f"✂️ جاري قص الجزء المطلوب ({mtool_mod.format_seconds(clip['start'])} → {mtool_mod.format_seconds(clip['end'])})..."
        )
        try:
            trimmed_path = await mtool_mod.trim_media_by_time(dl.file_path, clip["start"], clip["end"], is_audio=dl.is_audio)
            _cleanup_file(dl.file_path)
            dl.file_path = trimmed_path
            cache_key = None  # لا نخزّن بالكاش نسخة مقصوصة تحت مفتاح الفيديو الكامل
        except Exception as e:
            logger.exception(f"[clip] فشل قص الجزء المطلوب | url={task['url']} | chat={chat_id}")
            await bot.edit_message_text(
                chat_id, msg["message_id"],
                f"⚠️ تعذّر قص الجزء المطلوب، سيُرسل الملف كاملاً:\n{str(e)[:150]}"
            )

    await _send_result(chat_id, dl, msg["message_id"], task, cache_key=cache_key)
    PENDING.pop(token, None)


def _cleanup_file(path: str):
    """يحذف الملف المؤقت بعد الإرسال ويسجّل النتيجة."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
            logger.info(f"[cleanup] 🧹 تم حذف الملف المؤقت: {path}")
    except Exception:
        logger.exception(f"[cleanup] فشل حذف الملف المؤقت: {path}")

# ══════════════════════════════════════════════
# /start و /plugins (أوامر أساسية مدمجة)
# ══════════════════════════════════════════════
async def cmd_start(msg: dict):
    chat_id = msg["chat"]["id"]
    plugins = get_plugins()
    sites   = "\n".join(
        f"   `•` *{p['name']}* — {p['description'] or ', '.join(p['domains'][:3])}"
        for p in plugins
    )
    text = (
        "🎬 *أهلاً بك في بوت تحميل الوسائط!*\n"
        "بوت واحد لتحميل الفيديو والصوت من عشرات المنصات، بجودة تختارها أنت.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ *ماذا يمكنني أن أفعل؟*\n\n"
        "📥 *أرسل رابطاً مباشرة*\n"
        "   تختار تنزيل الرابط كاملاً، أو جزءاً محدداً بالوقت، ثم أعرض لك كل الجودات المتاحة (فيديو 🎥 وصوت 🎵).\n\n"
        "🔎 *أرسل اسم أغنية بدون رابط*\n"
        "   أبحث لك في عدة منصات وأعرض أفضل 10 نتائج لتختار منها.\n\n"
        "🎧 *أرسل مقطع صوت/فيديو مباشرة*\n"
        "   أعرض لك قائمة: اكتشاف الأغنية (Shazam)، عرض الكلمات، تحويل الفيديو إلى صوت، أو قص جزء منه بالزمن.\n\n"
        "📝 *استخدم* `/lyrics <اسم الأغنية>`\n"
        "   أو رُد بـ `/lyrics` على مقطع صوتي/فيديو لعرض كلماته (تعرّف تلقائي أولاً).\n\n"
        "⚡ *إرسال فوري للطلبات المكرَّرة*\n"
        "   أي رابط سبق تحميله بنفس الجودة يُعاد إرساله فوراً دون انتظار.\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔌 *المواقع المدعومة حالياً:*\n{sites}\n\n"
        "✅ جرّب الآن — فقط أرسل رابطاً أو اسم أغنية!"
    )
    await bot.send_message(chat_id, text, parse_mode="Markdown")

async def cmd_plugins(msg: dict):
    chat_id = msg["chat"]["id"]
    reg = get_registry()
    lines = []
    for name, info in reg.items():
        status = "✅" if info["status"] == "loaded" else "❌"
        lines.append(f"{status} {name}: {info.get('description') or info.get('reason','')}")
    await bot.send_message(chat_id, "🔌 الـ Plugins:\n\n" + "\n".join(lines))

_BUILTIN_COMMANDS = {"start": cmd_start, "plugins": cmd_plugins}

# ══════════════════════════════════════════════
# 🧭 توجيه التحديثات الخام إلى المعالج المناسب
# ══════════════════════════════════════════════
async def _dispatch_update(update: dict):
    try:
        if "callback_query" in update:
            cq   = update["callback_query"]
            data = cq.get("data") or ""
            if data.startswith("dl|"):
                await handle_choice(cq)
            elif data.startswith("srch|"):
                await handle_search_choice(cq)
            elif data.startswith("mode|"):
                await handle_url_mode_choice(cq)
            else:
                # بادئات أخرى (مثل mtool| من plugins/media_tools.py) تُوجَّه لأي
                # extra_handler من الـ plugins يقبلها عبر فلتره الخاص
                for h in get_extra_handlers():
                    try:
                        if h["filter"](cq):
                            await h["callback"](cq, bot)
                            break
                    except Exception:
                        logger.exception("[handler-plugin] فشل فحص/تنفيذ handler لـ callback_query")
            return

        if "message" not in update:
            return
        msg = update["message"]

        if is_command(msg):
            name = command_name(msg)
            if name in _BUILTIN_COMMANDS:
                await _BUILTIN_COMMANDS[name](msg)
                return
            # أوامر إضافية من الـ plugins (مثل /lyrics) — تُطابق عبر filter الخاص بها
            for h in get_extra_handlers():
                try:
                    if h["filter"](msg):
                        await h["callback"](msg, bot)
                        return
                except Exception:
                    logger.exception(f"[handler-plugin] فشل فحص/تنفيذ handler لأمر: {name}")
            return

        if is_plain_text(msg):
            # نعطي extra_handlers فرصة أولاً (مثلاً رد بوقت قص مُنتظَر من
            # plugins/media_tools.py)؛ إن لم يطالب بها أحد نُكمل للمسار الافتراضي
            for h in get_extra_handlers():
                try:
                    if h["filter"](msg):
                        await h["callback"](msg, bot)
                        return
                except Exception:
                    logger.exception("[handler-plugin] فشل فحص/تنفيذ handler لرسالة نصية")
            await handle_message(msg)
            return

        # رسائل وسائط مباشرة (صوت/فيديو) → handler-plugins مثل media_tools
        for h in get_extra_handlers():
            try:
                if h["filter"](msg):
                    await h["callback"](msg, bot)
                    return
            except Exception:
                logger.exception("[handler-plugin] فشل فحص/تنفيذ handler لرسالة وسائط")

    except Exception:
        logger.exception(f"⚠️ خطأ غير متوقع أثناء معالجة update={update}")

# ══════════════════════════════════════════════
# Webhook / Sanic
# ══════════════════════════════════════════════
async def _periodic_cleanup():
    """تنظيف دوري لـ PENDING/SEARCH_PENDING حتى لو البوت خامل بدون طلبات جديدة."""
    while True:
        await asyncio.sleep(5 * 60)
        try:
            _cleanup()
            _cleanup_search()
            _cleanup_url_mode()
        except Exception:
            logger.exception("[cleanup] فشل التنظيف الدوري")

@app.before_server_start
async def setup(_app):
    load_all_plugins()
    await run_pending_setups()
    await cache.init_cache(config)
    if get_extra_handlers():
        logger.info(f"✅ تم تسجيل {len(get_extra_handlers())} handler إضافي من الـ plugins")
    await bot.set_webhook(f"{config.SERVER_URL}{config.WEBHOOK_PATH}")
    app.add_task(_periodic_cleanup())
    logger.info("✅ البوت يعمل!")

@app.after_server_stop
async def teardown(_app):
    await close_http_session()
    await cache.close_cache()

@app.post(config.WEBHOOK_PATH)
async def webhook(request):
    try:
        upd = request.json
        if upd:
            await _dispatch_update(upd)
    except Exception:
        logger.exception("webhook: خطأ أثناء معالجة التحديث")
    return sanic_response.json({"ok": True})

@app.get("/")
async def index(request):
    return sanic_response.json({"status": "online", "plugins": get_registry()})

@app.get("/health")
async def health(request):
    return sanic_response.json({"status": "healthy", "ts": time.time()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, access_log=False)
