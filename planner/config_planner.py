"""
إعدادات المخطط الأسبوعي/الشهري وربط Google Sheets.

هذا الملف منفصل تمامًا عن config.py (الخاص بخط إنتاج الفيديو: نماذج Gemini،
عتبات المونتاج والتدقيق...) — يخص فقط طبقة التخطيط/الجدولة/تتبع الأداء/التعلّم،
ولا يُقرأ من main.py مطلقًا في مسار الرندر نفسه.
"""

# ===== Google Sheets =====
SPREADSHEET_ID_ENV = "SPREADSHEET_ID"                  # نفس السر الموجود مسبقًا في GitHub Secrets
SERVICE_ACCOUNT_JSON_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"  # نفس السر الموجود مسبقًا

TAB_CONFIG = "Config"   # صف حالة واحد: رقم الأسبوع، الفئات النشطة، هل تم القفل عليها، حالة التعلّم المستمر
TAB_PLAN = "Plan"       # خطة المنشورات القادمة (موضوع + فئة + موعد نشر + بيانات وسم للتعلّم)
TAB_STATS = "Stats"     # بيانات الفيديوهات المنشورة فعليًا + أداؤها (views/likes/comments/retention)

# ===== الفئات (9 فئات بعد استبعاد "حِيَل الحياة اليومية ونصائح الإنتاجية") =====
CATEGORY_LABELS = {
    "space": "Space & astronomy facts",
    "science": "General science & how things work",
    "nature": "Nature & non-violent wildlife",
    "ocean": "Ocean & deep sea life",
    "geography": "Geography & unusual/amazing places on Earth",
    "architecture": "Architecture & world wonders",
    "human_body": "Human body general facts (non-medical-advice)",
    "inventions": "Inventions & history of scientific discoveries",
    "general_facts": "General surprising / interesting facts",
}
CATEGORY_POOL = list(CATEGORY_LABELS.keys())  # 9 فئات

# اختبار 3 فئات أسبوعيًا (يدور على كل الـ9 كل 3 أسابيع = "جولة" اختبار كاملة).
# الفرق عن التصميم السابق: هذا يتكرر لعدة جولات تلقائيًا (بدل التوقف بعد جولة
# واحدة فقط/14 فيديو لكل فئة) حتى تتحقق دلالة إحصائية حقيقية أو يُبلغ سقف أمان.
CATEGORIES_PER_TEST_WEEK = 3
TEST_WAVES = [CATEGORY_POOL[i:i + CATEGORIES_PER_TEST_WEEK]
              for i in range(0, len(CATEGORY_POOL), CATEGORIES_PER_TEST_WEEK)]  # 3 مجموعات × 3 فئات

# ===== عتبات القفل الإحصائي (تحل محل "3 أسابيع ثابتة/14 فيديو" في التصميم الأصلي) =====
# أقل عدد فيديوهات لكل فئة قبل حتى التفكير بالقفل عليها. 28 = جولتا اختبار
# (أسبوعان لكل فئة على مدى 6 أسابيع تقويمية)، بدل أسبوع واحد/14 فيديو سابقًا -
# استجابة مباشرة لملاحظة "ارفع حجم العيّنة، الأفضل 4-6 أسابيع".
MIN_VIDEOS_PER_CATEGORY_BEFORE_LOCK = 28
# سقف أمان: لو تجاوزنا هذا العدد من أسابيع الاختبار بلا دلالة إحصائية واضحة
# (احتمال حقيقي مع فئات متقاربة الأداء)، نقفل بأفضل ما هو متاح بدل الانتظار
# للأبد، لكن نُسجّل تحذيرًا صريحًا أن القرار غير مؤكّد إحصائيًا 100%.
MAX_TEST_WEEKS = 12
SIGNIFICANCE_ALPHA = 0.05  # عتبة قيمة-p القياسية لاعتبار الفرق "دالًا" وليس ضجيجًا

# ===== التعلّم المستمر بعد القفل (يحل محل "استغلال بحت أبدي" في التصميم الأصلي) =====
# نسبة من كل خطة شهرية تُخصَّص لفئة "تجريبية" خارج الثلاث المقفلة، لاكتشاف أي
# تحوّل لاحق بذوق الجمهور بدل التجمّد على قرار قديم للأبد.
EXPLORATION_RATE = 0.10
# كل هذا العدد من الأيام، يُعاد تحليل: هل فئة تجريبية تفوّقت إحصائيًا على أضعف
# فئة مقفلة؟ إن نعم، تستبدلها تلقائيًا (مع تسجيل السبب في Config.notes).
RE_EVALUATION_INTERVAL_DAYS = 30
MIN_VIDEOS_PER_CHALLENGER_BEFORE_SWAP = 15  # أقل عيّنة لفئة تجريبية قبل مقارنتها إحصائيًا بفئة مقفلة
# عند إعادة التقييم الدورية، نقارن فقط بيانات آخر هذا العدد من الأيام (نافذة
# متحركة) لا كل التاريخ منذ البداية - وإلا فأداء قديم (من قبل أي تحوّل بالذوق)
# يُخفّف (dilutes) أي إشارة تحوّل حديثة فعلية، ما يُبطئ اكتشافها - كما ورد في
# الملاحظة الأصلية "لو ذاق الجمهور تغيّر بعد شهرين ما فيه آلية تكتشف التراجع".
RE_EVALUATION_WINDOW_DAYS = 90

# قواعد أمان المحتوى نفسها المتفق عليها سابقًا، تُحقن داخل كل برومبت تخطيط
# حتى لا يقترح جمناي مواضيع تخرج عنها (أشخاص حقيقيون/وهميون، عنف، إلخ).
CONTENT_SAFETY_RULES = """
Strict content rules (must NEVER be violated by any suggested topic):
- English language only.
- No violence, death, war, crime, or weapons.
- No real or fictional named characters (human or animal) - a topic must be about
  a phenomenon, place, object, animal-in-general, or abstract fact, NOT a specific
  named person (no biography-style topics about a named scientist/celebrity/historical figure).
- No nudity, sexuality, or suggestive content of any kind.
- No alcohol, gambling, or drugs.
- Nothing graphic, disturbing, or politically/socially controversial.
- Every topic must be realistically findable as real-world stock footage on
  Pexels/Pixabay (physical objects, nature, places, science demos) - avoid purely
  abstract concepts with no visual representation.
"""

# ===== الجدولة =====
VIDEOS_PER_DAY = 2
DAYS_PER_WEEK = 7
VIDEOS_PER_WEEK = VIDEOS_PER_DAY * DAYS_PER_WEEK  # 14

# أوقات مستهدفة بتوقيت شرق أمريكا (يتحول تلقائيًا لتوقيت UTC الصحيح حسب DST
# لأننا نستخدم ZoneInfo وليس إزاحة ثابتة).
US_TIMEZONE = "America/New_York"
# التصميم السابق: وقتان ثابتان للأبد بلا أي مقارنة. الآن: بنك من الأوقات
# المرشّحة، ونظام bandit (planner/stats_math.py) يختار VIDEOS_PER_DAY منها
# يوميًا بالاعتماد على composite_score التاريخي لكل نافذة، مع استكشاف عشوائي
# دائم (SLOT_EXPLORATION_EPSILON) حتى لا يتجمّد على أول نتيجة جيدة بالصدفة.
CANDIDATE_SLOT_TIMES_ET = ["09:00", "12:30", "15:30", "17:30", "19:30", "21:30"]
SLOT_EXPLORATION_EPSILON = 0.20
SLOT_JITTER_MINUTES = 12  # عشوائية بسيطة حول الوقت المحدد (مضاد لنمط "نشر آلي بالضبط")

# أسماء مراحل Gemini الجديدة (تُضاف إلى STAGE_KEY_MAP في config.py بشكل تكميلي)
STAGE_PLANNER = "content_planner"
STAGE_ANALYZER = "category_analyzer"
