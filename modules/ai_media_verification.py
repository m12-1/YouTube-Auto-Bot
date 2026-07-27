"""
المرحلة 5: التحقق البصري من الوسائط.

يرسل أول 3 مرشحين (فيديو فعلي، وليس وصفًا نصيًا فقط) في طلب Gemini واحد لتقليل
عدد الطلبات، ويطلب تقييم كل مرشح (0-10) + أفضل نطاق زمني (best_segment).
- score >= MEDIA_RELEVANCE_ACCEPT_THRESHOLD -> قبول، وتُعلَّم الكلمة المفتاحية "مستخدمة".
- غير ذلك -> تجربة نتيجة تالية بنفس الكلمة، ثم الموقع الآخر، ثم طلب كلمة مفتاحية بديلة
  من Gemini (نفس مفتاح هذه المرحلة) وإعادة البحث، حتى MAX_KEYWORD_RETRY_PER_SCENE.
"""

import asyncio
import logging

from shared.gemini_client import call_gemini_with_rotation, parse_json_response
from shared.state import RunState
from modules.media_downloader import search_scene_media_sync, download_candidate
from config import (
    MEDIA_RELEVANCE_ACCEPT_THRESHOLD,
    MAX_CANDIDATES_PER_VERIFICATION_BATCH,
    MAX_KEYWORD_RETRY_PER_SCENE,
)

logger = logging.getLogger("modules.ai_media_verification")

STAGE = "ai_media_verification"

_VERIFY_PROMPT = """
لدي {n} مقاطع فيديو مرشحة لهذا الجزء من سكربت فيديو قصير:
النص: "{narration_excerpt}"

لكل فيديو (بالترتيب الذي أُرسل به)، قيّم مدى ملاءمته من 0 إلى 10 لتمثيل هذا النص بصريًا،
وحدد أفضل نطاق زمني (بالثواني) داخل الفيديو يمثل جوهر المشهد (لا يتجاوز طول المقطع الفعلي).

أعد **فقط** كائن JSON بهذا الشكل، بدون أي نص إضافي:
{{
  "results": [
    {{"candidate_index": 0, "score": 0, "best_segment": {{"start": 0.0, "end": 0.0}}}}
  ]
}}
"""

_ALT_KEYWORD_PROMPT = """
الكلمات المفتاحية التالية لم تُعطِ نتائج بصرية مناسبة لهذا النص: "{narration_excerpt}"
الكلمات التي جُرِّبت وفشلت: {tried_keywords}

اقترح 3 كلمات/جمل بحث بصري إنجليزية جديدة ومختلفة تمامًا عن السابقة، مناسبة للبحث
في مكتبات فيديو مجانية (Pexels/Pixabay) وتعبّر عن نفس المعنى.

أعد فقط كائن JSON: {{"alternative_keywords": ["...", "...", "..."]}}
"""


def _verify_batch(narration_excerpt: str, candidates: list[dict], local_paths: list[str]) -> list[dict]:
    prompt = _VERIFY_PROMPT.format(n=len(candidates), narration_excerpt=narration_excerpt)
    raw = call_gemini_with_rotation(
        STAGE, [prompt], media_paths=local_paths, response_mime_type="application/json"
    )
    return parse_json_response(raw).get("results", [])


def _request_alternative_keywords(narration_excerpt: str, tried_keywords: list[str]) -> list[str]:
    prompt = _ALT_KEYWORD_PROMPT.format(
        narration_excerpt=narration_excerpt, tried_keywords="، ".join(tried_keywords)
    )
    raw = call_gemini_with_rotation(STAGE, [prompt], response_mime_type="application/json")
    return parse_json_response(raw).get("alternative_keywords", [])


def verify_scene_media(scene: dict, run_state: RunState, media_dir: str) -> dict | None:
    """
    يشغّل حلقة كاملة للمشهد: بحث -> تحميل أول 3 -> تحقق دفعة واحدة -> قبول/رفض ->
    تبديل كلمة مفتاحية عند الحاجة. يعيد dict {clip_path, start, end, score} أو None عند الفشل التام.
    """
    scene_id = scene["id"]
    narration_excerpt = scene["text"]
    keywords_pool = list(scene.get("visual_keywords", []))

    for retry in range(MAX_KEYWORD_RETRY_PER_SCENE):
        remaining_keywords = [
            kw for kw in keywords_pool if not run_state.keyword_already_tried(scene_id, kw)
        ]
        if not remaining_keywords:
            logger.warning("[%s] لا كلمات مفتاحية متبقية، طلب بدائل من Gemini.", scene_id)
            tried = run_state.data["used_keywords"].get(scene_id, [])
            remaining_keywords = _request_alternative_keywords(narration_excerpt, tried)
            keywords_pool.extend(remaining_keywords)

        candidates = search_scene_media_sync(remaining_keywords)
        if not candidates:
            logger.warning("[%s] لم يُعثر على أي مرشح لهذه الجولة.", scene_id)
            for kw in remaining_keywords:
                run_state.mark_keyword_used(scene_id, kw)
            continue

        top3 = candidates[:MAX_CANDIDATES_PER_VERIFICATION_BATCH]

        async def _download_all(cands):
            return [await download_candidate(c, media_dir) for c in cands]

        local_paths = asyncio.run(_download_all(top3))

        try:
            results = _verify_batch(narration_excerpt, top3, local_paths)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] فشل التحقق من الدفعة: %s", scene_id, e)
            results = []

        accepted = [r for r in results if r.get("score", 0) >= MEDIA_RELEVANCE_ACCEPT_THRESHOLD]
        for kw in {c["keyword"] for c in top3}:
            run_state.mark_keyword_used(scene_id, kw)

        if accepted:
            best = max(accepted, key=lambda r: r["score"])
            idx = best["candidate_index"]
            chosen_candidate = top3[idx]
            chosen_path = local_paths[idx]
            segment = best["best_segment"]
            result = {
                "clip_path": chosen_path,
                "start": segment["start"],
                "end": segment["end"],
                "score": best["score"],
            }
            run_state.set_scene_result(scene_id, **result)
            logger.info("[%s] قُبل بعد %d محاولة (score=%.1f).", scene_id, retry + 1, best["score"])
            return result

        logger.info("[%s] رُفضت كل المرشحين في هذه الجولة (محاولة %d/%d).",
                    scene_id, retry + 1, MAX_KEYWORD_RETRY_PER_SCENE)

    logger.error("[%s] فشل نهائي بعد %d محاولات.", scene_id, MAX_KEYWORD_RETRY_PER_SCENE)
    return None
