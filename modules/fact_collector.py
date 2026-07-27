"""
المرحلة 2: جمع حقائق خام عن الموضوع من Wikipedia.
"""

import logging
import wikipedia

from shared.gemini_client import call_gemini_with_rotation

logger = logging.getLogger("modules.fact_collector")

# نستخدم مفتاح/نموذج مرحلة اختيار الموضوع نفسها (topic_selector) لأن هذه
# الحقائق الاحتياطية خفيفة الحجم ولا تحتاج مفتاحًا مخصصًا في STAGE_KEY_MAP.
_STAGE_FOR_FALLBACK = "topic_selector"

_FALLBACK_FACTS_PROMPT = """
أعطني حقائق موجزة ودقيقة (بالعربية) حول الموضوع التالي، بصيغة نثرية متصلة
(وليس نقاطًا)، بطول {sentences} جمل تقريبًا، مناسبة لتُستخدم كأساس لسكربت فيديو
تعليمي قصير:

الموضوع: "{topic}"

لا تذكر أنك نموذج ذكاء اصطناعي ولا تُضف أي تعليق خارج الحقائق نفسها.
"""


def _collect_facts_via_gemini(topic: str, sentences: int = 8) -> str:
    """احتياطي: عندما لا توجد أي نتيجة على Wikipedia (عربي أو إنجليزي) —
    وهو وارد لمواضيع مصاغة بشكل مبالغ في التحديد أو غير مطابق لعناوين
    ويكيبيديا — نطلب من Gemini نفسه حقائق موجزة بدل إيقاف خط الأنابيب بالكامل."""
    prompt = _FALLBACK_FACTS_PROMPT.format(topic=topic, sentences=sentences)
    logger.warning("لا نتائج Wikipedia لـ '%s' في أي لغة، التحول لتوليد حقائق عبر Gemini.", topic)
    return call_gemini_with_rotation(_STAGE_FOR_FALLBACK, [prompt])


def collect_facts(topic: str, lang: str = "ar", sentences: int = 8) -> str:
    """
    يعيد نصًا خامًا من Wikipedia حول الموضوع. يجرب العربية أولًا ثم الإنجليزية كبديل.
    إن لم توجد أي نتيجة في أي من اللغتين (مواضيع نادرة أو مصاغة بدقة غير موجودة
    حرفيًا على ويكيبيديا)، يتحول تلقائيًا لتوليد الحقائق عبر Gemini بدل رمي خطأ
    يوقف خط الأنابيب بالكامل.

    يستخدم wikipedia.search() أولًا بدل الاعتماد مباشرة على auto_suggest في summary()،
    لأن مكتبة wikipedia تحتوي على خلل (bug) يرمي IndexError بدل PageError عندما لا توجد
    أي نتائج بحث إطلاقًا (الوصول لعنصر أول في قائمة نتائج فارغة).
    """
    wikipedia.set_lang(lang)
    try:
        search_results = wikipedia.search(topic)
    except Exception as e:  # noqa: BLE001
        logger.warning("فشل البحث في Wikipedia (%s) عن '%s': %s", lang, topic, e)
        search_results = []

    if not search_results:
        if lang != "en":
            logger.warning("لا نتائج بحث عربية لـ '%s'، المحاولة بالإنجليزية.", topic)
            return collect_facts(topic, lang="en", sentences=sentences)
        return _collect_facts_via_gemini(topic, sentences=sentences)

    try:
        return wikipedia.summary(search_results[0], sentences=sentences, auto_suggest=False)
    except wikipedia.DisambiguationError as e:
        if e.options:
            return wikipedia.summary(e.options[0], sentences=sentences, auto_suggest=False)
        return _collect_facts_via_gemini(topic, sentences=sentences)
    except (wikipedia.PageError, IndexError):
        # حاول بقية النتائج قبل الانتقال للغة أخرى
        for alt_title in search_results[1:]:
            try:
                return wikipedia.summary(alt_title, sentences=sentences, auto_suggest=False)
            except Exception:  # noqa: BLE001
                continue
        if lang != "en":
            logger.warning("تعذّر جلب أي صفحة عربية مطابقة لـ '%s'، المحاولة بالإنجليزية.", topic)
            return collect_facts(topic, lang="en", sentences=sentences)
        return _collect_facts_via_gemini(topic, sentences=sentences)


if __name__ == "__main__":
    print(collect_facts("الثقوب السوداء"))
