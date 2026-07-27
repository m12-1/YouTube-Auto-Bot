"""
المرحلة 2: جمع حقائق خام عن الموضوع من Wikipedia.
"""

import logging
import wikipedia

logger = logging.getLogger("modules.fact_collector")


def collect_facts(topic: str, lang: str = "ar", sentences: int = 8) -> str:
    """
    يعيد نصًا خامًا من Wikipedia حول الموضوع. يجرب العربية أولًا ثم الإنجليزية كبديل.

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
        raise ValueError(f"لا توجد نتائج Wikipedia (عربي أو إنجليزي) للموضوع: '{topic}'")

    try:
        return wikipedia.summary(search_results[0], sentences=sentences, auto_suggest=False)
    except wikipedia.DisambiguationError as e:
        if e.options:
            return wikipedia.summary(e.options[0], sentences=sentences, auto_suggest=False)
        raise
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
        raise


if __name__ == "__main__":
    print(collect_facts("الثقوب السوداء"))
