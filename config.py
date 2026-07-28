"""
الإعدادات المركزية للمشروع.
"""

# ===== سلسلة نماذج Gemini المعتمدة (من الأفضل إلى الأقل) — جيل 3 فقط =====
MODEL_CHAIN = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]

# ===== تخصيص كل مرحلة بمفتاح Gemini أساسي =====
STAGE_KEY_MAP = {
    "topic_selector":           "GEMINI_KEY_LIGHT",
    "script_and_seo_planner":   "GEMINI_KEY_ADVANCED",
    "ai_media_verification":    "GEMINI_KEY_FILTER",
    "final_scene_audit":        "GEMINI_KEY_FILTER_2",
    "competitor_seo_optimizer": "GEMINI_KEY_IMAGE",
    # مرحلتا التخطيط (planner/) - إضافة تكميلية فقط، لا تمس مراحل الإنتاج أعلاه
    "content_planner":          "GEMINI_KEY_LIGHT",     # بناء خطة أسبوعية/شهرية - مهمة بسيطة
    "category_analyzer":        "GEMINI_KEY_ADVANCED",  # تحليل أداء الفئات - يحتاج استدلالًا أعمق
}

# ترتيب المفاتيح الاحتياطي العالمي (يُستخدم عند نفاد كل نماذج المفتاح الأساسي للمرحلة)
ALL_KEYS_ORDER = [
    "GEMINI_KEY_ADVANCED",
    "GEMINI_KEY_FILTER",
    "GEMINI_KEY_FILTER_2",
    "GEMINI_KEY_IMAGE",
    "GEMINI_KEY_LIGHT",
]

# ===== حدود الفيديو =====
MIN_VIDEO_SECONDS = 30
MAX_VIDEO_SECONDS = 60
EXPORT_RESOLUTION = (1080, 1920)  # عمودي لليوتيوب شورتس
FADE_DURATION = 0.25              # ثواني، انتقال تلاشي سريع

# ===== عتبات التقييم =====
MEDIA_RELEVANCE_ACCEPT_THRESHOLD = 8       # من 10 - قبول المشهد من نتائج البحث
SCENE_AUDIT_ACCEPT_THRESHOLD = 7.5         # من 10 - قبول المشهد في التدقيق النهائي
MAX_CANDIDATES_PER_VERIFICATION_BATCH = 5  # عدد الفيديوهات المرسلة دفعة واحدة للتقييم (رُفعت من 3 بعد إضافة 3 مصادر جديدة)

# ===== حدود إعادة المحاولة =====
MAX_KEYWORD_RETRY_PER_SCENE = 4   # عدد محاولات تبديل الكلمة المفتاحية لكل مشهد (نقطة 5)
MAX_SCENE_AUDIT_RETRIES = 3       # عدد محاولات إعادة بناء مشهد مرفوض (نقطة 7)

# الحد الأدنى المطلق المقبول لخطة fallback (أفضل مرشح لم يبلغ عتبة القبول
# العادية MEDIA_RELEVANCE_ACCEPT_THRESHOLD). أي مرشح fallback بدرجة أقل من هذا
# الحد لا يُقبل بصمت، بل يُعلَّم المشهد بعلم needs_manual_review.
MIN_FALLBACK_ACCEPT_SCORE = 6

# ===== الفئات التي يجب جلب فيديوهاتها من ناسا (فضاء/فلك) =====
SPACE_ASTRONOMY_CATEGORY_KEYWORDS = [
    "فضاء", "فلك", "كواكب", "نجوم", "مجرة", "مجرات", "كون",
    "space", "astronomy", "planet", "planets", "galaxy", "galaxies",
    "universe", "nasa", "cosmos", "star", "stars",
]

# ===== عدد المقاطع المطلوب جلبها من كل مصدر لكل كلمة مفتاحية =====
CANDIDATES_PER_KEYWORD_PER_SOURCE = 3   # نقطة 4: لكل كلمة 3 مقاطع من كل مصدر

# الحد الأدنى لدقة الفيديو (الارتفاع بالبكسل) المقبول من أي مصدر - نفس الحد
# المطبّق على Pexels/Pixabay، ويُطبَّق أيضًا على Internet Archive وWikimedia
# Commons لتفادي المسحات القديمة/الملفات منخفضة الجودة.
MIN_ACCEPTABLE_VIDEO_HEIGHT = 1080

# ===== فلتر اتجاه الفيديو (عمودي/أفقي) =====
# القيم الممكنة: "portrait" (عمودي، ارتفاع > عرض) و"landscape" (أفقي، عرض >
# ارتفاع). حاليًا نقبل العمودي فقط (شورتس)؛ لإضافة الأفقي مستقبلاً يكفي
# إضافة "landscape" لهذه القائمة دون أي تعديل آخر في الكود.
ALLOWED_VIDEO_ORIENTATIONS = ["portrait"]

# ===== يوتيوب SEO =====
TOP_COMPETITOR_VIDEOS = 5
