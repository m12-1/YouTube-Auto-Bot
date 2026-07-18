"""
script_writer.py
يكتب سكربت الفيديو الطويل (5 دقائق ~750-800 كلمة) وسكربت الشورت (~120 كلمة)
لنفس الموضوع، بنية story-driven مع hook قوي بأول 5 ثواني.
"""
import json
from scripts import config, gemini_client

LONG_SCRIPT_PROMPT = """
اكتب سكربت فيديو يوتيوب معلوماتي مشوّق باللغة الإنجليزية عن الموضوع التالي:
"{topic}"

الشروط الصارمة:
- المدة المستهدفة: 5 دقائق قراءة بصوت طبيعي (~750-800 كلمة إنجليزية)
- أول جملة يجب أن تكون hook صادم يخلق فضول فوري (لا مقدمات عامة، لا "hello guys welcome back")
- بنية: hook -> 4 نقاط رئيسية مبنية كقصة متصاعدة -> خاتمة تترك تأثيراً + سؤال للتفاعل بالكومنتات
- قسّم النص لفقرات، كل فقرة تمثل "مشهد" بصري مستقل (سنربطها بصور لاحقاً)
- ممنوع أي معلومة غير مؤكدة تُقدَّم كحقيقة قطعية — استخدم "studies suggest" ونحوها عند الشك
- ممنوع ذكر أي شخصية سياسية أو حدث إخباري حساس
- ممنوع منعاً باتاً أي إشارة لـ: الكحول/الخمور، المخدرات، العري أو المحتوى
  الجنسي، القمار، أو أي محتوى مخالف للقيم الإسلامية

أرجع JSON فقط بهذا الشكل:
{{
  "title_draft": "...",
  "hook": "...",
  "scenes": [
    {{"scene_number": 1, "narration": "...", "visual_keywords": ["...", "..."]}},
    ...
  ],
  "closing_cta": "..."
}}
"""

SHORT_SCRIPT_PROMPT = """
حوّل هذا الموضوع لسكربت YouTube Short (أقل من 60 ثانية، ~120 كلمة إنجليزية)،
بأسلوب أسرع وأكثر صدمة من الفيديو الطويل، hook بأول ثانيتين إجباري.
الموضوع: "{topic}"

ممنوع منعاً باتاً أي إشارة (ولو غير مباشرة) لـ: الكحول/الخمور، المخدرات،
العري أو المحتوى الجنسي، القمار، أو أي محتوى آخر مخالف للقيم الإسلامية.
أيضاً ممنوع أي محتوى سياسي أو إخباري حساس.

أرجع JSON فقط:
{{"narration": "...", "visual_keywords": ["...", "..."]}}
"""


def write_long_script(topic: str) -> dict:
    prompt = LONG_SCRIPT_PROMPT.format(topic=topic)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    return json.loads(raw)


def write_short_script(topic: str) -> dict:
    prompt = SHORT_SCRIPT_PROMPT.format(topic=topic)
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    return json.loads(raw)


def full_narration_text(long_script: dict) -> str:
    """يجمع كل السكربت كنص متصل لتمريره لـ TTS."""
    parts = [long_script["hook"]]
    parts += [s["narration"] for s in long_script["scenes"]]
    parts.append(long_script["closing_cta"])
    return " ".join(parts)
