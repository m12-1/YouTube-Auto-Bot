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
import os
import subprocess
import uuid

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

# نضغط كل مرشح لمعاينة صغيرة قبل إرساله لـ Gemini، لأن الملفات الأصلية
# (خصوصًا من Pixabay) غالبًا ما تتجاوز حد الـ inline data لدى Gemini (~18MB).
# المسار الأصلي كامل الجودة يبقى محفوظًا ويُستخدم لاحقًا في المونتاج النهائي.
_PREVIEW_MAX_SECONDS = 12
_PREVIEW_SCALE_HEIGHT = 360
_PREVIEW_VIDEO_BITRATE = "250k"

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


def _interleave_candidates(candidates: list[dict]) -> list[dict]:
    """يخيّر المرشحين حسب (كلمة مفتاحية، مصدر) بدل تركهم بترتيب البحث الخام،
    كي لا تستهلك أول 3 مرشحين فقط نتائج Pexels/الكلمة الأولى ويُهمَل Pixabay."""
    by_kw_source = {}
    for c in candidates:
        by_kw_source.setdefault((c["keyword"], c["source"]), []).append(c)
    buckets = list(by_kw_source.values())
    result = []
    i = 0
    while any(i < len(b) for b in buckets):
        for b in buckets:
            if i < len(b):
                result.append(b[i])
        i += 1
    return result


def _make_verification_preview(source_path: str, media_dir: str) -> str | None:
    """
    يولّد نسخة معاينة صغيرة (مدة أقصر + دقة/بترييت أقل + بدون صوت) من الفيديو
    الأصلي عبر ffmpeg، لإرسالها لـ Gemini كـ inline data ضمن حد الحجم المسموح،
    مع الإبقاء على المسار الأصلي كامل الجودة لاستخدامه في المونتاج النهائي.
    يعيد None إن فشل الضغط لأي سبب (ffmpeg غير متاح، ملف تالف، ...).
    """
    preview_dir = os.path.join(media_dir, "previews")
    os.makedirs(preview_dir, exist_ok=True)
    out_path = os.path.join(preview_dir, f"{uuid.uuid4().hex}.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-t", str(_PREVIEW_MAX_SECONDS),
        "-vf", f"scale=-2:{_PREVIEW_SCALE_HEIGHT}",
        "-b:v", _PREVIEW_VIDEO_BITRATE,
        "-an", "-movflags", "+faststart",
        out_path,
    ]
    try:
        subprocess.run(
            cmd, check=True, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("فشل توليد معاينة مضغوطة لـ %s: %s", source_path, e)
        return None

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.warning("معاينة فارغة/غير موجودة لـ %s.", source_path)
        return None
    return out_path


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

        candidates = _interleave_candidates(candidates)
        top3 = candidates[:MAX_CANDIDATES_PER_VERIFICATION_BATCH]

        async def _download_all(cands):
            paths = []
            for c in cands:
                try:
                    paths.append(await download_candidate(c, media_dir))
                except Exception as e:  # noqa: BLE001
                    logger.warning("فشل تحميل %s: %s", c.get("url"), e)
                    paths.append(None)
            return paths

        local_paths = asyncio.run(_download_all(top3))

        # نصفّي المرشحين الذين فشل تحميلهم فعليًا قبل التحقق (بدل إرسال مسارات
        # مفقودة لـ Gemini)، مع إعادة بناء top3/local_paths بشكل متطابق الفهارس.
        valid_pairs = [(c, p) for c, p in zip(top3, local_paths) if p is not None]
        if not valid_pairs:
            logger.warning("[%s] فشل تحميل كل مرشحي هذه الجولة.", scene_id)
            for kw in {c["keyword"] for c in top3}:
                run_state.mark_keyword_used(scene_id, kw)
            continue
        top3 = [c for c, _ in valid_pairs]
        local_paths = [p for _, p in valid_pairs]

        # نولّد معاينة مضغوطة من كل فيديو لإرسالها لـ Gemini (بدل الملف الأصلي
        # الذي غالبًا يتجاوز حد الـ inline data)، مع الإبقاء على local_paths
        # الأصلية كاملة الجودة لاستخدامها لاحقًا في المونتاج النهائي.
        preview_paths = [
            _make_verification_preview(p, media_dir) or p for p in local_paths
        ]

        try:
            results = _verify_batch(narration_excerpt, top3, preview_paths)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] فشل التحقق من الدفعة: %s", scene_id, e)
            results = []

        accepted = [r for r in results if r.get("score", 0) >= MEDIA_RELEVANCE_ACCEPT_THRESHOLD]
        for kw in {c["keyword"] for c in top3}:
            run_state.mark_keyword_used(scene_id, kw)

        # نرتّب المقبولين تنازليًا حسب score ونختار أول مرشح صالح فعليًا
        # (تحقق من صحة candidate_index وتوفر segment وأن التحميل لم يفشل)
        accepted_sorted = sorted(accepted, key=lambda r: r.get("score", 0), reverse=True)
        best = None
        idx = None
        segment = None
        for cand in accepted_sorted:
            cand_idx = cand.get("candidate_index")
            cand_segment = cand.get("best_segment")
            if (
                cand_idx is None
                or not (0 <= cand_idx < len(top3))
                or local_paths[cand_idx] is None
                or not cand_segment
            ):
                logger.warning("[%s] استجابة تحقق غير صالحة (index=%s)، تجاهل.", scene_id, cand_idx)
                continue
            best, idx, segment = cand, cand_idx, cand_segment
            break

        if best is not None:
            chosen_candidate = top3[idx]
            chosen_path = local_paths[idx]
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
