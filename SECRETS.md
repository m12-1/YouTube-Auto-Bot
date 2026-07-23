# 🔐 قائمة الأسرار المطلوبة (GitHub Secrets)

اذهب إلى: **Repo Settings → Secrets and variables → Actions → New repository secret**
وأضف كل سر بالاسم بالضبط كما هو مكتوب هنا (حساس لحالة الأحرف).

---

## 1. مفاتيح Gemini (4 مفاتيح منفصلة)

| الاسم | الوظيفة الوحيدة | من أين |
|---|---|---|
| `GEMINI_KEY_LIGHT` | فلترة الترندات + Quality Gate + Embeddings + استبدال كلمات مفتاحية | https://aistudio.google.com/apikey — مشروع GCP أول (Project A) |
| `GEMINI_KEY_ADVANCED` | كتابة السكربت + SEO + التشخيص الذاتي | نفس الرابط — مشروع GCP ثانٍ (Project B) |
| `GEMINI_KEY_IMAGE` | ⭐ **مفتاح جوكر احتياطي** — يُستخدم تلقائياً لو أي مفتاح آخر توقف عن العمل (كان مخصصاً لتوليد الصور لكن أُلغيت) | نفس الرابط — مشروع GCP ثالث (Project C) |
| `GEMINI_KEY_FILTER` | التحقق البصري من تطابق الوسائط مع النص فقط (اختياري — لو تُرك فارغاً يُستخدم ADVANCED) | نفس الرابط — مشروع GCP رابع أو نفس مشروع آخر |
| `GEMINI_KEY_FILTER_2` | ⭐ **اختياري** — مفتاح ثانٍ إضافي لمضاعفة حصة التحقق البصري اليومية فقط. لو أُضيف، يُعاد تجربة كل نماذج التحقق البصري عليه تلقائياً بعد استنفاد `GEMINI_KEY_FILTER`، قبل اللجوء لـ Groq/Puter/CLIP | نفس الرابط — أنشئ مشروع GCP خامس منفصل تماماً (حتى لا يشارك نفس حصة أي مفتاح آخر) |

⚠️ **لازم 3 مشاريع GCP منفصلة فعلياً** (لا يكفي 3 مفاتيح من نفس المشروع) حتى تحصل على 3 حصص طلبات منفصلة فعلياً.

يوجد أيضاً `GEMINI_KEY_FILTER` اختياري مخصص للتحقق البصري من الوسائط (لو تُرك فارغاً يُستخدم `GEMINI_KEY_ADVANCED` بدلاً عنه تلقائياً).

---

## 1.5. مفتاح Groq (طبقة تحليل بصري ثانية احتياطية)

| الاسم | الاستخدام | من أين |
|---|---|---|
| `GROQ_API_KEY` | طبقة ثانية للتحقق البصري من تطابق الوسائط مع النص، تُستخدم فقط لو فشلت **كل** نماذج Gemini الأربعة، وقبل اللجوء لحارس CLIP المحلي كخط دفاع أخير | https://console.groq.com/keys — تسجيل حساب مجاني ثم "Create API Key" |

راجع `scripts/groq_client.py` لتفاصيل الآلية (تستخدم نموذج `qwen/qwen3.6-27b`، وتستخرج إطاراً كل 5 ثوانٍ من الفيديوهات تلقائياً لأن Groq لا يقبل ملفات فيديو مباشرة).

---

## 1.7. Puter AI (طبقة تحليل بصري ثالثة)

| الاسم | الاستخدام | من أين |
|---|---|---|
| `PUTER_USERNAME` | إيميل حساب Puter — طبقة ثالثة للتحقق البصري تعمل بعد فشل Groq وقبل CLIP المحلي | https://puter.com — أنشئ حساب مجاني |
| `PUTER_PASSWORD` | كلمة مرور حساب Puter | نفس الحساب أعلاه |

⬇️ **أين تضع الإيميل والرمز:**
- **GitHub Secrets:** أضف `PUTER_USERNAME` (الإيميل) و `PUTER_PASSWORD` (كلمة المرور) كأسرار منفصلة
- **التشغيل المحلي:** في ملف `.env` أضف نفس المتغيرين

Puter يعمل كوسيط للوصول لنماذج AI (مثل Gemini) بحصة مجانية منفصلة تماماً عن مفاتيح Google الخاصة بك. راجع `scripts/puter_client.py` لتفاصيل الآلية.

---

## 2. مفاتيح YouTube (اثنان بأدوار مختلفة تماماً)

### أ) `YOUTUBE_SEARCH_API_KEY`
مفتاح API بسيط (Public API Key)، **بدون** OAuth، يكفي للقراءة العامة فقط (trending + بحث المنافسين).
- اذهب لـ https://console.cloud.google.com
- أنشئ مشروع (يُفضّل مشروع رابع منفصل عن مشاريع Gemini)
- فعّل **YouTube Data API v3**
- Credentials → Create Credentials → API Key

### ب) OAuth للرفع الفعلي (`videos.insert` يحتاج تفويض حساب حقيقي، المفتاح البسيط لا يكفي)
- `YOUTUBE_OAUTH_CLIENT_ID`
- `YOUTUBE_OAUTH_CLIENT_SECRET`
- `YOUTUBE_OAUTH_REFRESH_TOKEN`

**خطوات الحصول عليها:**
1. بنفس مشروع GCP، من Credentials → Create Credentials → **OAuth Client ID** → نوع "Desktop App"
2. حمّل ملف JSON، فيه `client_id` و `client_secret`
3. شغّل مرة واحدة يدوياً سكربت تفويض OAuth (Google يوفر أمثلة جاهزة باسم `get_refresh_token.py` لـ YouTube API) على جهازك، سجّل دخول بحساب القناة، وافق على الصلاحيات — سيعطيك `refresh_token` تحفظه مرة واحدة فقط ويستمر يعمل تلقائياً بعدها بدون تدخل يدوي مجدداً.

---

## 3. مصادر الصور

| الاسم | من أين |
|---|---|
| `PIXABAY_API_KEY` | https://pixabay.com/api/docs/ — مجاني، تسجيل حساب فقط |
| `PEXELS_API_KEY` | https://www.pexels.com/api/ — مجاني، تسجيل حساب فقط |

---

## 4. Google Sheets (قاعدة البيانات)

### `GOOGLE_SERVICE_ACCOUNT_JSON`
- اذهب لنفس أو مشروع GCP جديد → IAM & Admin → Service Accounts → Create Service Account
- أنشئ مفتاح JSON له (Keys → Add Key → JSON) — **انسخ محتوى الملف كامل كنص وحيد السطر بهذا السر**
- فعّل **Google Sheets API** و **Google Drive API** لنفس المشروع
- **الأهم:** افتح جدول Google Sheets الخاص بك، وشارك (Share) الجدول مع الإيميل الموجود داخل ملف JSON (يشبه `xxx@xxx.iam.gserviceaccount.com`) بصلاحية **Editor**، وإلا السكربت ما راح يقدر يوصل للجدول.

### `SPREADSHEET_ID`
هو الجزء من رابط الجدول بين `/d/` و `/edit`:
`https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit`

**لازم تنشئ الجدول يدوياً بـ 4 أوراق (worksheets) بهذه الأسماء بالضبط:**
- `Current_Plan`
- `Daily_Log` (أعمدة: video_id, title, status, published_at)
- `Trend_Log` (أعمدة: date, chosen_title, core_topic, reason)
- `System_Control` (خلية A1 تحتوي `ON` أو `OFF`)

---

## 5. تليجرام (التنبيهات)

| الاسم | من أين |
|---|---|
| `TELEGRAM_BOT_TOKEN` | تحدث مع [@BotFather](https://t.me/BotFather) على تليجرام، أرسل `/newbot`، سيعطيك التوكن |
| `TELEGRAM_CHAT_ID` | أرسل أي رسالة للبوت الجديد، ثم افتح: `https://api.telegram.org/bot<TOKEN>/getUpdates` وابحث عن رقم `"chat":{"id": ...}` |

---

## 6. GitHub

### `GH_PAT`
Personal Access Token يستخدمه `self_heal.py` لإنشاء فروع/Pull Requests تلقائياً عند الأخطاء.
- GitHub → Settings (حسابك الشخصي) → Developer settings → Personal access tokens → Fine-grained tokens
- صلاحيات: `Contents: Read and write`, `Pull requests: Read and write` على هذا الريبو تحديداً

---

## ✅ ملخص سريع — كل الأسرار (16 سر)

```
GEMINI_KEY_LIGHT
GEMINI_KEY_ADVANCED
GEMINI_KEY_IMAGE
GEMINI_KEY_FILTER
GEMINI_KEY_FILTER_2 (اختياري)
GROQ_API_KEY
PUTER_USERNAME
PUTER_PASSWORD
YOUTUBE_SEARCH_API_KEY
YOUTUBE_OAUTH_CLIENT_ID
YOUTUBE_OAUTH_CLIENT_SECRET
YOUTUBE_OAUTH_REFRESH_TOKEN
PIXABAY_API_KEY
PEXELS_API_KEY
GOOGLE_SERVICE_ACCOUNT_JSON
SPREADSHEET_ID
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
GH_PAT
```

(18 سر فعلياً بالعد)
