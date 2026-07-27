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
        avoid_clause = "Avoid these topics since they were used recently: " + ", ".join(recent_topics)

    prompt = f"""
You are a topic-picking assistant for short YouTube videos (Shorts), for an English-speaking audience.
Category: {category}
Suggest exactly one specific topic, explainable in 30-60 seconds, interesting and not too generic.
{avoid_clause}
Return only the topic name as a single line of text, in English, with no extra explanation or punctuation.
"""
    result = call_gemini_with_rotation(STAGE, [prompt], max_output_tokens=300, temperature=0.9)
    first_line = next((line for line in result.strip().splitlines() if line.strip()), result)
    return first_line.strip().strip('"').strip("*").strip()


if __name__ == "__main__":
    print(select_topic("amazing science facts"))
