"""
المرحلة 2: جمع حقائق خام عن الموضوع من Wikipedia.
"""

import logging
import wikipedia

logger = logging.getLogger("modules.fact_collector")


def collect_facts(topic: str, lang: str = "ar", sentences: int = 8) -> str:
    """
    يعيد نصًا خامًا من Wikipedia حول الموضوع. يجرب العربية أولًا ثم الإنجليزية كبديل.
    """
    wikipedia.set_lang(lang)
    try:
        return wikipedia.summary(topic, sentences=sentences, auto_suggest=True)
    except wikipedia.DisambiguationError as e:
        # يأخذ أول خيار من قائمة الاحتمالات
        if e.options:
            return wikipedia.summary(e.options[0], sentences=sentences, auto_suggest=False)
        raise
    except wikipedia.PageError:
        if lang != "en":
            logger.warning("لم يُعثر على صفحة عربية لـ '%s'، المحاولة بالإنجليزية.", topic)
            return collect_facts(topic, lang="en", sentences=sentences)
        raise


if __name__ == "__main__":
    print(collect_facts("الثقوب السوداء"))
