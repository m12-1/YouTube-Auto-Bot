# YouTube Auto Bot

خط أنابيب Python كامل لتوليد ونشر فيديوهات يوتيوب شورتس تلقائيًا، وفق تسعة مراحل
كما ورد في المواصفة، مع نظام تدوير مفاتيح/نماذج Gemini ذكي.

⚠️ **ملاحظة مهمة**: هذا بناء أولي كامل للمعمارية المتفق عليها، مبني من الصفر بناءً
على وصفك التفصيلي — لأنني لم أستلم ملف zip فعليًا لمشروعك الحالي في هذه المحادثة
(فقط النص الذي وصفت فيه الخطة). إن كان لديك أكواد موجودة تريد الحفاظ على أجزاء
منها (مثل `script_generator.py` الأصلي أو نماذج بيانات معينة)، أرسل الـ zip
وسأدمج/أعدّل الملفات المحددة مباشرة بدل إعادة الكتابة الكاملة.

## البنية

```
youtube-auto-bot/
├── main.py                          # المنسّق الرئيسي (يشغل كل المراحل 1-9)
├── config.py                        # سلسلة النماذج، توزيع المفاتيح، العتبات
├── requirements.txt
├── .env.example                     # كل الأسرار المطلوبة
├── shared/
│   ├── gemini_client.py             # تدوير المفاتيح × النماذج + fallback عند 429
│   └── state.py                     # تتبع الكلمات المفتاحية المستخدمة/حالة التشغيل
├── modules/
│   ├── topic_selector.py            # 1) اختيار الموضوع (GEMINI_KEY_LIGHT)
│   ├── fact_collector.py            # 2) حقائق من Wikipedia
│   ├── script_and_seo_planner.py    # 3) سكربت + مشاهد + عنوان (GEMINI_KEY_ADVANCED)
│   ├── media_downloader.py          # 4) بحث متوازٍ Pexels+Pixabay (asyncio)
│   ├── ai_media_verification.py     # 5) تحقق بصري دفعة (3 فيديوهات) (GEMINI_KEY_FILTER)
│   ├── voice_generator.py           # Edge-TTS + word boundaries
│   ├── subtitle_generator.py        # ترجمة مقسّمة حسب علامات الترقيم
│   ├── video_composer.py            # مونتاج + fade + تصدير 1080x1920
│   ├── final_scene_audit.py         # 7) تدقيق نهائي لكل مشهد (GEMINI_KEY_FILTER_2)
│   ├── competitor_seo_optimizer.py  # 8) SEO مقارنة بالمنافسين (GEMINI_KEY_IMAGE)
│   └── publisher.py                 # 9) نشر يوتيوب (refresh_token فعلي)
└── .github/workflows/pipeline.yml   # تشغيل مجدول عبر GitHub Actions
```

## الإعداد

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg imagemagick   # مطلوب لـ moviepy و TextClip

cp .env.example .env
# املأ كل القيم في .env (أو أضفها كـ GitHub Secrets لتشغيل Actions)
```

## التشغيل المحلي

```bash
python main.py --category "علوم وحقائق مذهلة" --dry-run   # بدون نشر فعلي
python main.py --category "علوم وحقائق مذهلة"              # مع النشر على يوتيوب
```

## نظام تدوير المفاتيح (`shared/gemini_client.py`)

- كل مرحلة لها مفتاح Gemini أساسي (`STAGE_KEY_MAP` في `config.py`).
- عند 429 على المفتاح الحالي: ينتقل للنموذج التالي في `MODEL_CHAIN` (نفس المفتاح).
- إذا نفدت كل نماذج المفتاح: ينتقل للمفتاح التالي في `ALL_KEYS_ORDER` من أول نموذج.
- الحد الأقصى: 5 مفاتيح × 5 نماذج = 25 محاولة قبل الفشل النهائي.
- مفتاح فارغ في البيئة يُتخطى فورًا دون استهلاك محاولة.

## نقاط تحتاج انتباهك قبل التشغيل الحقيقي

1. **أصوات Edge-TTS**: الصوت الافتراضي `ar-SA-HamedNeural` في `voice_generator.py` —
   بدّله حسب تفضيلك (`edge-tts --list-voices` لعرض الخيارات).
2. **حدود Wikipedia العربية**: بعض المواضيع التقنية قد لا يكون لها صفحة عربية جيدة؛
   الكود يتحول تلقائيًا للإنجليزية عند الفشل، لكن هذا يعني أن `facts` قد تأتي بالإنجليزية
   والسكربت في المرحلة 3 سيحتاج ترجمتها ضمن نفس البرومبت (تم تضمين ذلك ضمنيًا لأن
   الطلب في البرومبت هو "عربية فصحى" بغض النظر عن لغة المصدر).
3. **أسماء نماذج Gemini 3.x** في `config.py` (`gemini-3.6-flash` إلخ) منسوخة من القائمة
   التي أرفقتها؛ تأكد من مطابقتها لأسماء الموديلات الفعلية المتاحة لحسابك عبر
   `genai.list_models()` قبل التشغيل الفعلي، لأن أسماء الإصدارات تتغير بسرعة.
4. **حصة YouTube Data API**: البحث عن منافسين (المرحلة 8) يستهلك quota يوميًا محدودًا
   (10000 وحدة، والبحث الواحد يكلف 100 وحدة تقريبًا) — راقب الاستهلاك إذا كان التشغيل يوميًا.
5. **moviepy + TextClip** يحتاج ImageMagick مثبتًا ومهيّأً بشكل صحيح على السيرفر/الرانر.

## الخطوة التالية المقترحة

أرسل الـ zip الفعلي لمشروعك الحالي إن كان لديك أكواد `gemini_manager.py` أو
`video_composer.py` سابقة تريد مطابقتها بدل استبدالها، وسأدمج التعديلات مباشرة على
ملفاتك بدل إرسال مشروع كامل من جديد في كل مرة (كما تفضّل عادة).
