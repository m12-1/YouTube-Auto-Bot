"""
content_policy.py
فلتر حظر صارم (hard block) يُستخدم بـ 3 نقاط مختلفة بخط الإنتاج (ترند + سكربت
+ Quality Gate) كطبقات حماية متعددة، وليس فقط اعتماداً على تقييم Gemini الذاتي.
يغطي: (أ) سياسة يوتيوب للمحتوى الحساس، (ب) طلبك الصريح لاستبعاد ما يخالف
الشريعة الإسلامية (الخمر، المخدرات، العري، القمار، الجنس).
"""

BLOCKED_TOPICS = {
    "alcohol": [
        "alcohol", "wine", "beer", "vodka", "whiskey", "whisky", "cocktail",
        "brewery", "champagne", "liquor", "drunk", "intoxicat",
    ],
    "drugs": [
        "drug", "cocaine", "heroin", "marijuana", "cannabis", "weed",
        "meth", "narcotic", "opioid", "vape", "smoking weed",
    ],
    "nudity_indecency": [
        "nude", "naked", "topless", "nsfw", "explicit content",
        "lingerie", "strip club", "twerk",
    ],
    "gambling": [
        "gambling", "casino", "betting", "poker", "lottery", "wager",
        "sports betting", "slot machine",
    ],
    "sexual": [
        "sex", "porn", "erotic", "adult content", "onlyfans", "hookup",
        "dating app", "affair",
    ],
    # مطابق لسياسة يوتيوب للمحتوى الحساس/غير الأصيل، أضيف لحمايتك من الحظر
    "news_politics_violence": [
        "election", "war", "shooting", "died", "death of", "controversy",
        "scandal", "lawsuit", "trial", "president", "senator", "terrorist",
        "murder", "suicide",
    ],
}


def contains_blocked_content(text: str) -> tuple[bool, str]:
    """يرجع (True, الفئة المخالفة) لو النص يحتوي كلمة محظورة، وإلا (False, '')."""
    lowered = text.lower()
    for category, keywords in BLOCKED_TOPICS.items():
        for kw in keywords:
            if kw in lowered:
                return True, category
    return False, ""


def all_blocked_keywords_flat() -> list[str]:
    flat = []
    for kws in BLOCKED_TOPICS.values():
        flat += kws
    return flat
