"""
المرحلة 3: تحويل الحقائق الخام إلى سكربت منظم + خطة مشاهد + SEO أولي.
تدمج (script_generator + script_reviewer + storyboard_generator) في استدعاء واحد.
مفتاح: GEMINI_KEY_ADVANCED.
"""

import json
import logging

from shared.gemini_client import call_gemini_with_rotation, parse_json_response
from config import MIN_VIDEO_SECONDS, MAX_VIDEO_SECONDS, HOOK_QUALITY_ACCEPT_THRESHOLD

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
   - visual_keywords: 3-4 stock-footage search queries (in English) sent
     directly to Pexels / Pixabay / NASA video APIs.

     Each query MUST follow the formula:
       [specific subject] + [visible action or state] + [setting / context]

     Rules for crafting these queries:
     a) Be CONCRETE and VISUAL — describe what the camera literally sees,
        NOT the abstract concept the narration discusses.
        BAD:  "black hole", "gravity", "space"
        GOOD: "stars swirling galactic center time-lapse",
              "massive star exploding supernova shockwave",
              "astronaut floating weightless inside space station"
     b) Include motion/action words whenever the scene implies movement:
        "waves crashing rocky coast slow motion",
        "blood cells flowing vein microscope close-up",
        "city traffic aerial timelapse night lights"
     c) Vary the angle — each query targets a DIFFERENT visual angle
        (establishing shot / close-up detail / action moment /
        data-visualization or diagram) so candidates are diverse.
     d) Do NOT add orientation words ("vertical", "portrait") — the API
        filters handle that automatically.
     e) Do NOT use SEO or marketing words ("amazing", "best", "incredible")
        — they corrupt stock-search ranking.
     f) Keep each query 3-6 words for maximum precision.
   - duration_estimate: estimated duration of this scene in seconds (decimal)
3. video_title: a catchy English video title
4. youtube_keywords: SEO keywords in English, ordered to match the narration flow
5. hook_self_review: after writing the narration above, critique your OWN hook
   (the first 2-3 seconds of narration) honestly, as a strict short-form-video
   editor would, using these exact criteria:
   a) Does it create a genuine curiosity gap that stays UNRESOLVED until later
      in the script (not a fact that's already fully explained in the hook itself)?
   b) Did it actually avoid generic/banned openers ("Did you know...", a flat
      topic description, or any template-sounding phrase)?
   c) Is it written specifically for THIS topic and these facts (not a generic
      sentence that could be reused for almost any topic by swapping one word)?
   Score your own hook honestly from 0 to 10 (score, not score of the whole
   script) — be a harsh critic, most first-draft hooks deserve 5-7, not 9-10.
   Return: {{"score": 0.0, "issue": "specific honest weakness if score < 8, else empty string"}}

Return **only** a JSON object in this exact shape, no extra text or Markdown fences:
{{
  "narration": "...",
  "scenes": [
    {{"id": "scene_1", "text": "...", "visual_keywords": ["subject action setting", "subject motion context", "subject detail close-up"], "duration_estimate": 5.0}}
  ],
  "video_title": "...",
  "youtube_keywords": ["...", "..."],
  "hook_self_review": {{"score": 0.0, "issue": "..."}}
}}
"""


def _total_duration(plan: dict) -> float:
    return sum(s.get("duration_estimate", 0) for s in plan.get("scenes", []))


def build_plan(topic: str, facts: str, max_retries: int = 4) -> dict:
    """
    يستدعي Gemini لبناء الخطة، ويتحقق من شرطين قبل القبول:
    1) مجموع مدة المشاهد ضمن [MIN, MAX] (كما كان).
    2) تقييم Gemini الذاتي لجودة الهوك (hook_self_review.score) >=
       HOOK_QUALITY_ACCEPT_THRESHOLD - نفس فلسفة إعادة استبدال المشهد المرفوض
       في التدقيق البصري النهائي، لكن مطبّقة هنا على الهوك النصي بدل اللقطة
       المرئية، وضمن نفس استدعاء بناء السكربت (بلا أي استدعاء Gemini إضافي).
    إن فشل أي شرط، تُدمَج كل الملاحظات في رسالة واحدة وتُطلب إعادة المحاولة،
    مع توضيح أنه لو كان الهوك فقط هو المشكلة، يُعاد كتابة الهوك (أول 2-3 ثوانٍ
    من السكربت) فقط بما يعالج الملاحظة ويناسب الموضوع تحديدًا، مع إبقاء بقية
    السكربت/المشاهد كما هي قدر الإمكان.
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
        duration_ok = MIN_VIDEO_SECONDS <= total <= MAX_VIDEO_SECONDS

        hook_review = plan.get("hook_self_review") or {}
        hook_score = hook_review.get("score", 10)
        hook_issue = hook_review.get("issue", "")
        hook_ok = hook_score >= HOOK_QUALITY_ACCEPT_THRESHOLD

        if duration_ok and hook_ok:
            plan["_total_duration"] = total
            return plan

        issues = []
        if not duration_ok:
            logger.warning(
                "محاولة %d: مجموع مدة المشاهد = %.1f ثانية (خارج النطاق %d-%d).",
                attempt + 1, total, MIN_VIDEO_SECONDS, MAX_VIDEO_SECONDS,
            )
            issues.append(
                f"- المدة: خطتك السابقة كانت مجموع مدتها {total:.1f} ثانية وهذا خارج النطاق "
                f"المطلوب ({MIN_VIDEO_SECONDS}-{MAX_VIDEO_SECONDS}). أعد التوزيع بدقة أكبر."
            )
        if not hook_ok:
            logger.warning(
                "محاولة %d: تقييم الهوك الذاتي = %.1f (دون العتبة %.1f). الملاحظة: %s",
                attempt + 1, hook_score, HOOK_QUALITY_ACCEPT_THRESHOLD, hook_issue or "(لا توجد)",
            )
            issues.append(
                f"- الهوك: قيّمتَ هوكك السابق بنفسك بـ {hook_score:.1f}/10 (دون العتبة "
                f"{HOOK_QUALITY_ACCEPT_THRESHOLD}) بسبب: \"{hook_issue}\". أعد كتابة الهوك "
                f"فقط (أول 2-3 ثوانٍ من السكربت) بما يعالج هذه الملاحظة تحديدًا ويناسب موضوع "
                f"\"{topic}\" بدقة أكبر - لا تكتب هوكًا عامًا قابلاً لإعادة استخدامه لأي موضوع "
                f"آخر. أبقِ بقية السكربت والمشاهد كما هي قدر الإمكان طالما تظل متماسكة مع الهوك "
                f"الجديد."
            )

        feedback = "\n\nملاحظات على المحاولة السابقة يجب معالجتها في هذه المحاولة:\n" + "\n".join(issues)

    # آخر محاولة: نعيد آخر خطة حتى لو لم تُطابق تمامًا، مع تحذير
    if plan is None:
        raise ValueError(f"فشل الحصول على JSON صالح بعد {max_retries} محاولات.")
    logger.error("لم يتم استيفاء شرط المدة و/أو جودة الهوك بعد %d محاولات، استخدام آخر نتيجة.", max_retries)
    plan["_total_duration"] = total
    return plan


if __name__ == "__main__":
    demo_facts = "الثقوب السوداء هي مناطق في الفضاء ذات جاذبية هائلة لدرجة أن الضوء لا يستطيع الإفلات منها."
    print(build_plan("الثقوب السوداء", demo_facts))
