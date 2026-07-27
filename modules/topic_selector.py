"""
المرحلة 1: اختيار الموضوع بناءً على فئة معينة، باستخدام مفتاح GEMINI_KEY_LIGHT
(نموذج خفيف — مهمة بسيطة لا تستحق نموذجًا متقدمًا).
"""

from shared.gemini_client import call_gemini_with_rotation

STAGE = "topic_selector"


def select_topic(category: str, recent_topics: list[str] | None = None) -> str:
    recent_topics = recent_topics or []
    avoid_clause = ""
    if recent_topics:
        avoid_clause = "تجنب هذه المواضيع لأنها استخدمت مؤخرًا: " + "، ".join(recent_topics)

    prompt = f"""
أنت مساعد اختيار مواضيع فيديوهات قصيرة (يوتيوب شورتس).
الفئة: {category}
اقترح موضوعًا واحدًا فقط، محددًا وقابلًا للشرح خلال 30-60 ثانية، مثير للاهتمام وغير عام جدًا.
{avoid_clause}
أعد فقط اسم الموضوع كسطر نصي واحد بدون أي شرح إضافي أو علامات ترقيم زائدة.
"""
    result = call_gemini_with_rotation(STAGE, [prompt], max_output_tokens=100, temperature=0.9)
    return result.strip().strip('"').strip()


if __name__ == "__main__":
    print(select_topic("علوم وحقائق مذهلة"))
