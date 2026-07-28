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
You are a scriptwriter for short, punchy educational YouTube Shorts aimed at an English-speaking audience.

Topic: {topic}
Raw facts (from Wikipedia):
\"\"\"{facts}\"\"\"

Write:
1. narration: the voice-over script, timed to read in {min_s}-{max_s} seconds
   (~2.3 words/second). Written in clear, natural, conversational English.

   Structure it as a mini narrative arc, NOT a flat list of disconnected facts:
   - HOOK (first 2-3 seconds): a provocative question, a shocking claim, or a
     "wait, what?" statement that stops someone mid-scroll. Do NOT start with
     generic phrases like "Did you know that..." or a flat description of the
     topic. The hook must create curiosity or tension that only gets resolved
     later in the script.
   - BUILD-UP: connect the facts to each other with a logical or cause-and-effect
     thread (this happens, which leads to that, which is why...) instead of
     presenting them as "Fact 1, Fact 2, Fact 3". Each sentence should make the
     viewer want the next one.
   - PAYOFF: land on the single most surprising or counter-intuitive detail as
     the climax of the script, not buried in the middle.
   - CLOSING LINE: end with a short, punchy line that reframes the topic or
     leaves a memorable thought — never a generic "and that's amazing!" or
     "subscribe for more" type closer.

   Variety requirement: do not reuse the same opening phrase, sentence rhythm,
   or closing line pattern that a generic script for this topic type would use.
   Each script must read as a distinct piece of writing with its own voice, not
   a value swapped into an identical template — this matters for the channel's
   long-term content-authenticity standing, not just viewer retention.
2. scenes: split narration into consecutive scenes, each with:
   - text: the portion of narration text for this scene
   - visual_keywords: 2-4 precise visual search terms (in English, suitable
     for searching Pexels/Pixabay stock footage)
   - duration_estimate: estimated duration of this scene in seconds (decimal)
3. video_title: a catchy English video title
4. youtube_keywords: SEO keywords in English, ordered to match the narration flow

Return **only** a JSON object in this exact shape, no extra text or Markdown fences:
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
