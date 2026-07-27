"""
المرحلة 3: تحويل الحقائق الخام إلى سكربت منظم + خطة مشاهد + SEO أولي.
تدمج (script_generator + script_reviewer + storyboard_generator) في استدعاء واحد.
مفتاح: GEMINI_KEY_ADVANCED.
"""

import json
import logging

from shared.gemini_client import call_gemini_with_rotation, parse_json_response
from config import MIN_VIDEO_SECONDS, MAX_VIDEO_SECONDS

logger = logging.getLogger("modules.script_and_seo_planner")

STAGE = "script_and_seo_planner"

_PROMPT_TEMPLATE = """
أنت كاتب سكربتات لفيديوهات يوتيوب شورتس تعليمية قصيرة.

الموضوع: {topic}
الحقائق الخام (من Wikipedia):
\"\"\"{facts}\"\"\"

اكتب:
1. narration: نص الشرح الذي سيُروى صوتيًا في الفيديو، بحيث تكون مدته عند القراءة
   بين {min_s} و {max_s} ثانية (بمعدل ~2.3 كلمة/ثانية تقريبًا). لغة عربية فصحى بسيطة وواضحة.
2. scenes: قسّم narration إلى مشاهد متتالية، ولكل مشهد:
   - text: جزء النص المقابل لهذا المشهد
   - visual_keywords: 2-4 كلمات/جمل بحث بصري دقيقة (بالإنجليزية، مناسبة للبحث في Pexels/Pixabay)
   - duration_estimate: تقدير مدة هذا المشهد بالثواني (رقم عشري)
3. video_title: عنوان جذاب للفيديو (عربي)
4. youtube_keywords: قائمة كلمات مفتاحية مرتبة حسب تسلسل الشرح للبحث عنها في يوتيوب (عربي/إنجليزي مختلط حسب الأنسب)

أعد **فقط** كائن JSON بهذا الشكل بدون أي نص إضافي أو أسوار Markdown:
{{
  "narration": "...",
  "scenes": [
    {{"id": "scene_1", "text": "...", "visual_keywords": ["...", "..."], "duration_estimate": 5.0}}
  ],
  "video_title": "...",
  "youtube_keywords": ["...", "..."]
}}
"""


def _total_duration(plan: dict) -> float:
    return sum(s.get("duration_estimate", 0) for s in plan.get("scenes", []))


def build_plan(topic: str, facts: str, max_retries: int = 3) -> dict:
    """
    يستدعي Gemini لبناء الخطة، ويتحقق أن مجموع مدة المشاهد ضمن [MIN, MAX].
    إن خرج عن النطاق يطلب من جمناي إعادة الضبط (validation loop).
    """
    feedback = ""
    plan = None
    total = 0
    for attempt in range(max_retries):
        prompt = _PROMPT_TEMPLATE.format(
            topic=topic, facts=facts, min_s=MIN_VIDEO_SECONDS, max_s=MAX_VIDEO_SECONDS
        ) + feedback

        raw = call_gemini_with_rotation(
            STAGE, [prompt], response_mime_type="application/json", max_output_tokens=4096
        )
        try:
            plan = parse_json_response(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("محاولة %d: JSON غير صالح (%s)، إعادة المحاولة.", attempt + 1, e)
            feedback = "\n\nملاحظة: ردّك السابق لم يكن JSON صالحًا بالكامل. أعد الإخراج بعناية."
            continue
        total = _total_duration(plan)

        if MIN_VIDEO_SECONDS <= total <= MAX_VIDEO_SECONDS:
            plan["_total_duration"] = total
            return plan

        logger.warning(
            "محاولة %d: مجموع مدة المشاهد = %.1f ثانية (خارج النطاق %d-%d)، إعادة الضبط.",
            attempt + 1, total, MIN_VIDEO_SECONDS, MAX_VIDEO_SECONDS,
        )
        feedback = (
            f"\n\nملاحظة: خطتك السابقة كانت مجموع مدتها {total:.1f} ثانية وهذا خارج النطاق "
            f"المطلوب ({MIN_VIDEO_SECONDS}-{MAX_VIDEO_SECONDS}). أعد التوزيع بدقة أكبر."
        )

    # آخر محاولة: نعيد آخر خطة حتى لو لم تُطابق تمامًا، مع تحذير
    if plan is None:
        raise ValueError(f"فشل الحصول على JSON صالح بعد {max_retries} محاولات.")
    logger.error("لم يتم ضبط المدة ضمن النطاق بعد %d محاولات، استخدام آخر نتيجة.", max_retries)
    plan["_total_duration"] = total
    return plan


if __name__ == "__main__":
    demo_facts = "الثقوب السوداء هي مناطق في الفضاء ذات جاذبية هائلة لدرجة أن الضوء لا يستطيع الإفلات منها."
    print(build_plan("الثقوب السوداء", demo_facts))
