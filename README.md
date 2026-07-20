# 🎬 Sunken Media Bot

بوت تيليجرام لتحميل الوسائط من عشرات المواقع والبحث عن الأغاني، مبني على نظام **plugins** قابل للتوسعة بلا حدود — أي موقع جديد يُضاف بملف Python واحد دون لمس أي كود أساسي.

يعتمد البوت على استدعاءات **Telegram Bot API خام** (عبر `telegram_api.py`) بدل مكتبة `python-telegram-bot` — استهلاك ذاكرة أقل وأسطح تحكم أبسط، مع الحفاظ على كل منطق البوت وبنية الـ plugins كما هي.

---

## ✨ المميزات

- 🔌 **نظام plugins ديناميكي** — أضف أو احذف موقعاً بمجرد إضافة/حذف ملف في `plugins/`
- ⚡ **بدون python-telegram-bot** — عميل Bot API خفيف مبني على نفس جلسة `aiohttp` المشتركة، بدون Job Queue أو Persistence أو Rate-Limiter غير مستخدَمة
- 🗂️ **تهيئة مركزية (`config.py`)** — كل توكن، رابط API، وإعداد كاش في مكان واحد، تُقرأ عبر `python-dotenv`
- 💾 **كاش وسائط بهاش قصير (8 أحرف)** — رابط + جودة سبق تحميلهما ← إعادة إرسال فورية عبر `file_id` المخزَّن بدل تحميل/رفع من جديد
- 🎛️ **قائمة جودات تفاعلية** — أزرار inline لاختيار جودة الفيديو أو الصوت، بـ `callback_data` مضغوط دائماً تحت حد تيليجرام (64 بايت)
- 🐳 **Dockerfile يتولّد تلقائياً** — الحزم المطلوبة تُجمع من الـ plugins وتُحقن في الصورة
- 🥇 **APIs خارجية أولاً** — يوتيوب/فيسبوك/ساوندكلاود عبر مزودين خارجيين بدون حظر IP
- 📡 **Webhook بدل Polling** — أسرع استجابة وأقل استهلاكاً للموارد
- 💽 **Streaming حقيقي للقرص** — لا يُحمَّل الفيديو كاملاً بالذاكرة، استهلاك RAM ثابت
- ✂️ **تقسيم تلقائي للملفات الكبيرة** — أي ملف يتجاوز 50MB يُقسَّم عبر `ffmpeg` إلى أجزاء قابلة للتشغيل وتُرسل تباعاً، بدل رفضه
- 🎧 **التعرف على الأغاني (Shazam)** — أرسل ملاحظة صوتية/فيديو/صوت مباشرة ليتعرف عليها البوت
- 📝 **عرض كلمات الأغاني (`/lyrics`)** — بالاسم مباشرة، أو رداً على مقطع صوتي/فيديو (يتعرف عليه عبر Shazam أولاً)
- 🔎 **بحث نصي متعدد المنصات** — أرسل اسم أغنية بدون رابط ليبحث عنها البوت (YouTube, SoundCloud) ويعرض أفضل 10 نتائج بتنسيق منسّق

---

## 🌐 المواقع المدعومة

| Plugin | المواقع | الطريقة |
|---|---|---|
| `youtube.py` | YouTube | APIs خارجية (ccproject + yt-dlp-stream) — لتفادي حظر IP الخاص باستضافات مثل Render |
| `facebook.py` | Facebook, fb.watch | facebook-video-download-api (فيديو) + yt-dlp (صوت واحتياطي) |
| `soundcloud.py` | SoundCloud | API مباشر (client_id ديناميكي مشترك مع البحث) |
| `ytdlp_generic.py` | كل الباقي: TikTok, Twitter/X, Instagram, Bilibili, b23.tv, Twitch, Reddit، وأي موقع آخر يدعمه yt-dlp | yt-dlp + aria2c (تحميل متوازٍ) — `DOMAINS=["*"]` |

> `ytdlp_generic.py` هو الملف الوحيد الذي يعتمد على مكتبة yt-dlp مباشرة لكل المواقع التي لا يحظر Render's IP الوصول إليها. المواقع التي تحتاج ترويسات HTTP خاصة (مثل `Referer` الذي يحتاجه Bilibili) مُعرَّفة داخل نفس الملف عبر `_SITE_EXTRA_HEADERS`.
> ⚠️ روابط قوائم التشغيل (playlists) تُعامَل كعنصرها الأول فقط — لا يوجد دعم لتنزيل قائمة كاملة كملف ZIP.

بالإضافة للجدول أعلاه:

| Plugin | النوع | الوصف |
|---|---|---|
| `shazam.py` | Handler-plugin | يتعرف على أي مقطع صوت/فيديو/voice note مُرسل مباشرة عبر Shazam |
| `lyrics.py` | Handler-plugin (أمر `/lyrics`) | يعرض كلمات الأغنية بالاسم، أو رداً على مقطع صوتي/فيديو عبر التعرف على الأغنية أولاً (يعيد استخدام `shazam.py`)، من مصدر lyrics.ovh |
| `search_youtube.py` | Search-plugin | يبحث عن اسم الأغنية على YouTube |
| `search_soundcloud.py` | Search-plugin | يبحث عن اسم الأغنية على SoundCloud |

---

## 🗂️ هيكل المشروع

```
bot/
├── main.py               ← ثابت — يوجّه التحديثات، يدير الكاش والتقسيم/الرفع
├── plugin_loader.py      ← ثابت — يكتشف plugins/*.py، يولّد Dockerfile
├── telegram_api.py       ← ثابت — عميل Bot API خفيف (بديل python-telegram-bot)
├── config.py             ← 🆕 مصدر الحقيقة الوحيد لكل متغيرات البيئة
├── cache.py              ← 🆕 طبقة كاش الوسائط (هاش قصير ↔ file_id) — SQLite/Redis
├── .env.example          ← 🆕 نسخه إلى .env واملأه بقيمك
├── docker-compose.yml    ← 🆕 تشغيل بأمر واحد (+ Redis اختياري)
├── Dockerfile            ← يُولَّد تلقائياً عند الإقلاع، لا تعدّله يدوياً
├── Dockerfile.base       ← قالب Dockerfile الثابت (الجزء الأساسي)
├── requirements.txt
└── plugins/
    ├── youtube.py
    ├── facebook.py
    ├── soundcloud.py
    ├── ytdlp_generic.py      ← يغطي Bilibili وكل بقية المواقع (yt-dlp + aria2c)
    ├── shazam.py             ← Handler-plugin (التعرف الصوتي عبر Shazam)
    ├── lyrics.py             ← Handler-plugin (أمر /lyrics)
    ├── search_youtube.py     ← Search-plugin
    ├── search_soundcloud.py  ← Search-plugin
    └── your_new_site.py      ← أضف أي موقع جديد هنا فقط
```

---

## 🗄️ طبقة الكاش (media_cache) — الشرح الكامل

### لماذا كاش أصلاً؟

Telegram يخزّن كل ملف رُفع إليه ويعطيه `file_id` دائم. إعادة إرسال نفس `file_id` **فورية** (لا رفع فعلي، لا استهلاك باندويدث)، بعكس تحميل نفس الرابط من جديد وتحويله ورفعه من الصفر. في بوت تحميل وسائط، من الشائع جداً أن يُرسِل عدة مستخدمين (أو نفس المستخدم) نفس الرابط أكثر من مرة — هذه الطبقة تجعل الطلب الثاني فورياً تقريباً.

### كيف يُبنى المفتاح؟

كل رابط يُحوَّل إلى **هاش قصير حتمي (8 أحرف hex من SHA-256)** عبر `cache.short_hash()`:

```python
token = cache.short_hash(url)   # نفس الرابط → نفس التوكن دائماً
```

هذا التوكن يُستخدم في **مكانين بنفس الوقت**:
1. كمفتاح `PENDING` في `main.py` (بدل `uuid.uuid4()` العشوائي سابقاً)
2. داخل `callback_data` لأزرار اختيار الجودة: `dl|{token}|{quality_key}`

### لماذا هذا يحل مشكلة حد الـ 64 بايت؟

تيليجرام يرفض أي `callback_data` أطول من 64 بايت. رابط كامل (`https://youtube.com/watch?v=...`) يتجاوز هذا الحد بسهولة. الهاش القصير (8 أحرف) + الفواصل + مفتاح الجودة (مثل `v_1080`) يبقى دائماً حول 16-20 بايت — هامش أمان كبير حتى مع مفاتيح جودة أطول مستقبلاً.

### أين تُخزَّن البيانات فعلياً؟

مفتاح الكاش الكامل هو `{token}:{quality_key}` (مثلاً `a1b2c3d4:v_720`) ← لأن نفس الرابط بجودتين مختلفتين ينتج ملفين مختلفين بـ `file_id` مختلف لكل منهما.

عند اختيار المستخدم لجودة معيّنة (`handle_choice` في `main.py`):
1. **فحص الكاش أولاً** (`cache.get_cached`) — إن وُجد `file_id` سابق لنفس (الرابط + الجودة) يُعاد إرساله فوراً عبر `send_cached_video/audio/document` (طلب JSON بسيط، بدون multipart، بدون رفع بايتات).
2. إن لم يوجد، أو كان الـ `file_id` المخزَّن لم يعد صالحاً (مثلاً بسبب انتهاء صلاحية نادر من طرف تيليجرام) → **تحميل عادي** كما كان، ثم تخزين `file_id` الجديد بعد الرفع الناجح.
3. **الملفات المُقسَّمة (أكبر من 50MB) لا تُخزَّن بالكاش** — لأنها تنتج عدة `file_id` (جزء لكل ملف) لا يناسبها مفتاح كاش واحد. هذا قرار تصميم مقصود يفضّل البساطة على تعقيد غير ضروري لحالة نادرة.

### اختيار الـ Backend: SQLite افتراضياً، Redis اختياري

| | SQLite (افتراضي) | Redis (اختياري) |
|---|---|---|
| **متى يُستخدم** | نسخة واحدة من البوت (الوضع الشائع لهذا النوع من المشاريع) | عدة نسخ من البوت خلف نفس الـ webhook (توسّع أفقي) |
| **البنية التحتية** | صفر — ملف واحد على القرص | يحتاج خادم Redis منفصل |
| **التفعيل** | تلقائي، لا إعداد | اضبط `REDIS_URL` في `.env` فقط |
| **الأمان عند الفشل** | — | عند فشل الاتصال بـ Redis وقت الإقلاع، يتراجع البوت تلقائياً لـ SQLite بدل التوقف الكامل |

كلا الـ backend يطبّقان نفس الواجهة (`CacheBackend` في `cache.py`)، فأي دعم مستقبلي لـ PostgreSQL مثلاً يُضاف بنفس الطريقة دون لمس أي كود يستخدم الكاش (`main.py` لا يعرف أي backend نشط، فقط يستدعي `get_cached`/`set_cached`).

### متغيرات البيئة الخاصة بالكاش

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `CACHE_ENABLED` | `true` | تعطيل كامل للكاش عبر ضبطه `false` |
| `CACHE_DB_PATH` | `media_cache.db` | مسار ملف SQLite (اجعله داخل volume في Docker — راجع `docker-compose.yml`) |
| `CACHE_TTL_DAYS` | `30` | مدة صلاحية كل إدخال كاش بالأيام (`0` = بلا انتهاء) |
| `CACHE_HASH_LEN` | `8` | طول الهاش المستخدم في PENDING وcallback_data |
| `REDIS_URL` | (فارغ) | اضبطه لتفعيل Redis بدل SQLite |

---

## ⚙️ التهيئة (`config.py` + `.env`)

كل متغيرات البيئة الآن مركزية في `config.py` — لا مزيد من `os.getenv()` متناثرة عبر `main.py` والـ plugins. لبدء الإعداد:

```bash
cp .env.example .env
# افتح .env واملأ TELEGRAM_TOKEN و SERVER_URL على الأقل
```

| المتغير | إلزامي؟ | الوصف |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | توكن البوت من @BotFather |
| `SERVER_URL` | ✅ | الرابط العام لسيرفرك (بدون `/` في النهاية) |
| `PORT` | ❌ | افتراضي `10000` |
| `WEBHOOK_PATH` | ❌ | افتراضي `/webhook` |
| `MAX_CONCURRENT_DOWNLOADS` | ❌ | أقصى عدد تحميلات متزامنة، افتراضي `2` |
| `UPLOAD_LIMIT_MB` | ❌ | حد الرفع قبل التقسيم، افتراضي `50` (حد تيليجرام الفعلي) |
| `PENDING_TTL_MIN` / `SEARCH_PENDING_TTL_MIN` | ❌ | مدة صلاحية طلبات الجودة/نتائج البحث المؤقتة بالدقائق |
| `YT_API_1` / `YT_API_2` / `FB_DOWNLOAD_API` / `LYRICS_API` | ❌ | روابط الـ APIs الخارجية — القيم الافتراضية تعمل جاهزة |
| `LOG_LEVEL` | ❌ | افتراضي `INFO` |
| *(متغيرات الكاش)* | ❌ | راجع الجدول في القسم أعلاه |

عند غياب `TELEGRAM_TOKEN` أو `SERVER_URL` يتوقف البوت فوراً برسالة واضحة (`config.validate()` تُستدعى عند الإقلاع) بدل فشل غامض لاحقاً.

---

## 🚀 التشغيل

### محلياً (بدون Docker)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # واملأه
python main.py
```

> تحتاج `ffmpeg` و`aria2` مثبتين محلياً (`apt install ffmpeg aria2` على Debian/Ubuntu) لأن `Dockerfile.base` هو من يثبّتهما داخل الحاوية فقط.

### عبر Docker Compose (موصى به)

```bash
cp .env.example .env   # واملأه
docker compose up -d --build
```

لتفعيل Redis كطبقة كاش مشتركة (فقط عند تشغيل عدة نسخ من البوت):

```bash
echo "REDIS_URL=redis://redis:6379/0" >> .env
docker compose --profile redis up -d --build
```

### النشر على Render (أو أي استضافة Docker)

1. ارفع المشروع على GitHub
2. أنشئ **Web Service** جديد واربطه بالـ repo، اختر **Docker** كبيئة تشغيل
3. أضف متغيرات البيئة من الجدول أعلاه (`TELEGRAM_TOKEN`, `SERVER_URL` على الأقل)
4. انشر — `Dockerfile` يُولَّد تلقائياً عند أول إقلاع من `Dockerfile.base` + متطلبات الـ plugins

**لا تحتاج Build Command أو Start Command** — كل شيء في `Dockerfile`.

---

## ✂️ تقسيم الملفات الكبيرة تلقائياً

حد رفع تيليجرام للبوتات هو **50MB** (قابل للتعديل عبر `UPLOAD_LIMIT_MB`). عند تجاوز الملف المحمَّل هذا الحد:

1. يُحسَب طول كل جزء تلقائياً بناءً على معدّل بت الملف (بهامش أمان 10%)
2. `ffmpeg -c copy -f segment` يقسّم الملف دون إعادة ترميز (سريع، بدون فقد جودة)
3. تُرسَل الأجزاء تباعاً بترقيم واضح: `العنوان (جزء 1/3)`، `(جزء 2/3)`... إلخ
4. تُحذف كل الملفات المؤقتة (الأصلي والأجزاء) بعد الإرسال
5. **لا تُخزَّن الملفات المُقسَّمة بالكاش** (راجع قسم الكاش أعلاه)

> ملاحظة: هذه الميزة تعمل فقط على الفيديو/الصوت — أي ملف يُرسَل كمستند (`is_document=True`) لا يُقسَّم ويُرفض إذا تجاوز الحد.

---

## 🔌 إضافة موقع جديد (URL-plugin)

أنشئ ملفاً في `plugins/your_site.py` بهذا الهيكل:

```python
from plugin_loader import ProbeResult, DownloadResult, QualityOption

DESCRIPTION    = "وصف الموقع"
DOMAINS        = ["example.com"]   # روابط يدعمها هذا الـ plugin
PRIORITY       = 20                # أقل = يُجرَّب أولاً (افتراضي 50)

# (اختياري) حزم يحتاجها الـ plugin — تُضاف تلقائياً لـ Dockerfile
DOCKERFILE_APT = ["some-lib"]          # حزم apt
DOCKERFILE_RUN = ["some-cli --setup"]  # أوامر shell
DOCKERFILE_PIP = ["some-package==1.0"] # حزم pip

async def probe(url: str) -> ProbeResult:
    # افحص الرابط وأرجع قائمة الجودات المتاحة
    return ProbeResult(
        title="عنوان الوسائط",
        options=[
            QualityOption(kind="video", label="🎥 720p", key="v_720"),
            QualityOption(kind="audio", label="🎵 128kbps", key="a_128"),
        ]
    )

async def download(url: str, choice: dict) -> DownloadResult:
    # نزّل الملف وأرجع مساره — يُستدعى فقط عند عدم وجود نتيجة مخزَّنة بالكاش
    key = choice["key"]   # مثال: "v_720" أو "a_128"
    ...
    return DownloadResult(file_path="/tmp/file.mp4", title="العنوان", is_audio=False)
```

**هذا كل شيء** — البوت يكتشف الملف تلقائياً عند إعادة التشغيل، و`Dockerfile` يتحدث بنفسه إن أعلنت عن `DOCKERFILE_*`، والكاش يعمل تلقائياً لأي plugin جديد دون أي كود إضافي (المنطق في `main.py` عام لكل الـ plugins). لا حاجة لتسجيل أي شيء داخل `main.py`.

### إضافة أمر جديد أو التقاط وسائط مباشرة (Handler-plugin)

Handler-plugin يمكنه التقاط أوامر تيليجرام (`/xxx`) أو رسائل وسائط مباشرة (صوت/فيديو مُرسل بدون أمر)، عبر `register_plugin()`:

```python
from telegram_api import is_command, command_name, command_args

def _is_my_command(msg: dict) -> bool:
    return is_command(msg) and command_name(msg) == "mycmd"

async def handle_my_command(msg: dict, bot):
    chat_id = msg["chat"]["id"]
    await bot.send_message(chat_id, "تم استقبال /mycmd!")

def register_plugin():
    return {"filter": _is_my_command, "callback": handle_my_command}
```

`msg` قاموس رسالة تيليجرام الخام، و`bot` هو نفس عميل `telegram_api.Bot` المستخدم في كل مكان (`send_message`, `edit_message_text`, `send_document/audio/video`, `send_cached_document/audio/video`, `download_file`...).

### إضافة مزود بحث جديد (Search-plugin)

```python
from plugin_loader import SearchResult

SEARCH_PRIORITY = 30   # أقل = يُعرض أولاً في نتائج البحث المدمجة

async def search(query: str) -> list[SearchResult]:
    # ابحث في منصتك وأرجع حتى 10 نتائج
    return [
        SearchResult(title="اسم الأغنية", url="https://...", source="MyPlatform",
                     duration="3:45", uploader="الفنان"),
        ...
    ]
```

نتائج كل مزودات البحث المُحمَّلة تُدمج تلقائياً في `main.py`، تُقتطع لأفضل 10، وتُعرض كأزرار — اختيار أحدها يمرّ تلقائياً بمسار `probe`/`download` العادي (والكاش) لنفس الـ plugin الذي يخدم رابط تلك النتيجة.

---

## 🐳 كيف يتولّد Dockerfile

```
Dockerfile.base  +  DOCKERFILE_APT/RUN/PIP من كل plugins/*.py
        ↓
   plugin_loader.py  (عند كل إقلاع)
        ↓
   Dockerfile  (يُكتب تلقائياً)
```

- إذا أضفت plugin يحتاج `aria2` ← يظهر في `Dockerfile` تلقائياً
- إذا حذفت plugin ← تختفي حزمه من `Dockerfile` في الإقلاع التالي
- لا إعادة كتابة إذا لم يتغير شيء (يتحقق بـ hash)

> ⚠️ **تحذير — حزم pip ثقيلة (numpy, pydantic, أي حزمة بها Rust wheel...):**
> توليد `Dockerfile` يحدث **داخل** الحاوية بعد إقلاعها، أي أنه يفيد فقط **البناء التالي**، وليس الحاوية الحالية.
> إذا كانت أول نشرة لِـ plugin جديد يحتاج حزمة ثقيلة (مثل `shazamio` التي تسحب `numpy`/`pydantic-core`)، فسيقع البوت في fallback التثبيت الديناميكي (`subprocess.check_call(pip install ...)`) وقت الإقلاع — وهذا يُوقف الحلقة غير المتزامنة (event loop) لعشرات الثواني، فيتجاوز مهلة Sanic لتأكيد إقلاع الـ worker (30 ثانية افتراضياً) ويُسقِط الحاوية في crash loop.
> **الحل: أضف أي حزمة pip ثقيلة مباشرة إلى `requirements.txt`** (وليس فقط `DOCKERFILE_PIP` في الـ plugin) حتى تُثبَّت وقت بناء الصورة لا وقت التشغيل. الحزم الخفيفة الصغيرة لا تحتاج هذا الاحتراز.

---

## 📝 ملاحظات ختامية

- حد تيليجرام للرفع: **50MB** (`UPLOAD_LIMIT_MB`) — الملفات الأكبر تُقسَّم تلقائياً عبر ffmpeg، عدا الملفات كمستندات التي تُرفض كما كانت
- `ytdlp_generic.py` يقبل `DOMAINS = ["*"]` أي يعمل كـ fallback لأي رابط لا يطالبه plugin آخر
- `/plugins` أمر في البوت يعرض حالة كل plugin المحمَّل
- `/lyrics <اسم الأغنية>` أو رداً على مقطع صوتي/فيديو بـ `/lyrics` — لعرض كلمات الأغنية
- كل التوكنات وأزرار الجودة تستخدم الآن هاشاً حتمياً بدل معرّف عشوائي — نفس الرابط دائماً بنفس المفتاح، وهو ما يُمكِّن كاش `file_id` من العمل بشفافية دون أي تغيير في تجربة المستخدم
