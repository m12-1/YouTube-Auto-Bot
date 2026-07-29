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
from concurrent.futures import ThreadPoolExecutor

from shared.gemini_client import call_gemini_with_rotation, parse_json_response
from shared.state import RunState
from modules.media_downloader import search_scene_media_sync, download_candidate, search_scene_images_sync
from config import (
    MEDIA_RELEVANCE_ACCEPT_THRESHOLD,
    MAX_CANDIDATES_PER_VERIFICATION_BATCH,
    MAX_KEYWORD_RETRY_PER_SCENE,
    MIN_FALLBACK_ACCEPT_SCORE,
    ALLOWED_VIDEO_ORIENTATIONS,
    ENABLE_CROSS_SCENE_MEDIA_DEDUP,
    MEDIA_IMAGE_RELEVANCE_ACCEPT_THRESHOLD,
    MIN_IMAGES_PER_SCENE,
    MAX_IMAGES_PER_SCENE,
    MAX_IMAGE_KEYWORD_RETRY_PER_SCENE,
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

معلومات كل مقطع كما وردت من مصدره (العنوان/الوصف إن توفّرا) - استخدمها مع
تحليلك البصري للفيديو نفسه معًا، فقد يكون الفيديو يمثّل شيئًا مختلفًا عمّا
يبدو عليه بصريًا لأول وهلة (مثال: مقطع "سوائل ملونة تجريدية" قد يكون فعليًا
عن ظاهرة علمية محددة يوضحها عنوانه، أو العكس - فيديو يبدو مناسبًا بصريًا لكن
عنوانه/وصفه يكشف أنه عن موضوع مختلف تمامًا):
{candidates_meta}

قاعدة صارمة قبل أي تقييم: إن كان النص يذكر كيانًا محددًا بالاسم (كوكب، جرم
سماوي، شخص، مكان، كائن...) وكان الفيديو (بصريًا، أو حسب عنوانه/وصفه) يُظهر
كيانًا آخر مختلفًا فعليًا (مثال: النص عن "الزهرة" لكن الفيديو لكوكب "بلوتو" أو
سطح "القمر")، فهذا خطأ فادح (Entity Mismatch) ويجب إعطاؤه 0 مهما بدا التطابق
البصري العام جذابًا (نفس الفئة "كوكب/فضاء" لا يكفي، الهوية يجب أن تتطابق
تحديدًا). لا تتساهل هنا حتى لو كان هذا أفضل مرشح متاح.

لكل فيديو (بالترتيب الذي أُرسل به)، قيّم مدى ملاءمته من 0 إلى 10 لتمثيل هذا النص بصريًا
(بعد تطبيق قاعدة تطابق الهوية أعلاه أولاً)،
وحدد أفضل نطاق زمني (بالثواني) داخل الفيديو يمثل جوهر المشهد (لا يتجاوز طول المقطع الفعلي).

أعد **فقط** كائن JSON بهذا الشكل، بدون أي نص إضافي:
{{
  "results": [
    {{"candidate_index": 0, "score": 0, "best_segment": {{"start": 0.0, "end": 0.0}}}}
  ]
}}
"""

_ALT_KEYWORD_PROMPT = """
موضوع الفيديو العام: "{video_topic}"

الكلمات المفتاحية التالية لم تُعطِ نتائج بصرية مناسبة لهذا الجزء من السكربت: "{narration_excerpt}"
الكلمات التي جُرِّبت وفشلت: {tried_keywords}

اقترح 5 كلمات/جمل بحث بصري إنجليزية جديدة، مرادفة أو قريبة المعنى من الكلمات
الفاشلة (وليس مواضيع مختلفة تمامًا)، بحيث تبقى مرتبطة بنفس فكرة هذا الجزء من
النص وبموضوع الفيديو العام أعلاه، لكنها أوسع/أعم بصريًا (مرادف أعم، أو مشهد
رمزي/توضيحي مرتبط بنفس المعنى) لزيادة فرصة إيجاد نتائج في مكتبات فيديو مجانية
(Pexels/Pixabay). تجنّب تمامًا أي كلمة تكرر حرفيًا أو شبه حرفيًا إحدى الكلمات
الفاشلة المذكورة أعلاه.

أعد فقط كائن JSON: {{"alternative_keywords": ["...", "...", "...", "...", "..."]}}
"""


_VERIFY_IMAGE_PROMPT = """
لدي {n} صور مرشحة (خطة بديلة صارمة بعد فشل إيجاد فيديو مناسب) لهذا الجزء من سكربت فيديو قصير:
النص: "{narration_excerpt}"

معلومات كل صورة كما وردت من مصدرها (العنوان/الوصف إن توفّرا) - استخدمها مع
تحليلك البصري للصورة نفسها معًا:
{candidates_meta}

قاعدة صارمة قبل أي تقييم: إن كان النص يذكر كيانًا محددًا بالاسم (كوكب، جرم
سماوي، شخص، مكان، كائن...) وكانت الصورة (بصريًا، أو حسب عنوانها/وصفها) تُظهر
كيانًا آخر مختلفًا فعليًا، فهذا خطأ فادح (Entity Mismatch) ويجب إعطاؤه 0 مهما
بدا التطابق البصري العام جذابًا. لا تتساهل هنا حتى لو كانت هذه أفضل صورة متاحة.

هذه صورة ثابتة ستُعرض لعدة ثوانٍ بدل فيديو، لذا يجب أن تكون مطابقتها لجوهر
النص شبه مثالية (كن صارمًا جدًا في التقييم، لا تُعط 8 أو 9 إلا لتطابق فعلي
واضح لا لبس فيه).

لكل صورة (بالترتيب الذي أُرسلت به)، قيّم مدى ملاءمتها من 0 إلى 10 لتمثيل هذا النص بصريًا
(بعد تطبيق قاعدة تطابق الهوية أعلاه أولاً).

أعد **فقط** كائن JSON بهذا الشكل، بدون أي نص إضافي:
{{
  "results": [
    {{"candidate_index": 0, "score": 0}}
  ]
}}
"""


def _verify_image_batch(narration_excerpt: str, candidates: list[dict], local_paths: list[str]) -> list[dict]:
    prompt = _VERIFY_IMAGE_PROMPT.format(
        n=len(candidates),
        narration_excerpt=narration_excerpt,
        candidates_meta=_format_candidates_meta(candidates),
    )
    raw = call_gemini_with_rotation(
        STAGE, [prompt], media_paths=local_paths, response_mime_type="application/json"
    )
    return parse_json_response(raw).get("results", [])


def _download_images_sync(candidates: list[dict], media_dir: str) -> list[str | None]:
    async def _download_all(cands):
        results = await asyncio.gather(
            *[download_candidate(c, media_dir) for c in cands],
            return_exceptions=True,
        )
        paths = []
        for c, r in zip(cands, results):
            if isinstance(r, Exception):
                logger.warning("فشل تحميل صورة %s: %s", c.get("url"), r)
                paths.append(None)
            else:
                paths.append(r)
        return paths
    return asyncio.run(_download_all(candidates))


def _try_image_fallback(scene: dict, run_state: RunState, media_dir: str,
                         category: str = "", topic: str = "") -> dict | None:
    """
    خطة بديلة صارمة تُستدعى فقط بعد استنفاد كل محاولات الفيديو العادية لمشهد
    ما دون بلوغ عتبة القبول. تبحث عن صور (بدل فيديو) بنفس المصادر ونفس فلاتر
    الجودة/الرخصة/تعارض الكيانات، وتقبل فقط صورًا بتقييم >= 
    MEDIA_IMAGE_RELEVANCE_ACCEPT_THRESHOLD (أعلى من عتبة الفيديو نفسها). يجمع
    3-4 صور مقبولة (MIN/MAX_IMAGES_PER_SCENE) لتُعرض بالتتابع (زوم بطيء) بديلاً
    عن الفيديو المفقود. إن لم يصل لعدد الصور الأدنى، يعيد None فيتراجع الكود
    المستدعي لمنطق fallback الفيديو القديم (المراجعة اليدوية) كما كان.
    """
    scene_id = scene["id"]
    narration_excerpt = scene["text"]
    video_topic = run_state.data.get("video_title", "")
    base_keywords = list(scene.get("visual_keywords", []))

    tried_keywords: list[str] = []
    accepted: list[tuple[float, str, dict]] = []  # (score, local_path, candidate)
    used_source_keys: set[str] = set()

    for round_idx in range(MAX_IMAGE_KEYWORD_RETRY_PER_SCENE):
        if len(accepted) >= MIN_IMAGES_PER_SCENE:
            break

        keywords = base_keywords if round_idx == 0 else _request_alternative_keywords(
            narration_excerpt, tried_keywords, video_topic
        )
        if not keywords:
            continue
        tried_keywords.extend(keywords)

        candidates = search_scene_images_sync(keywords, category=category, topic=topic)
        if not candidates:
            logger.info("[%s] خطة الصور: لا نتائج للكلمات %s.", scene_id, keywords)
            continue

        if ENABLE_CROSS_SCENE_MEDIA_DEDUP:
            candidates = [c for c in candidates if not run_state.source_already_used(_source_key(c))]
        candidates = [c for c in candidates if _source_key(c) not in used_source_keys]
        if not candidates:
            continue

        batch = candidates[:MAX_CANDIDATES_PER_VERIFICATION_BATCH]
        local_paths = _download_images_sync(batch, media_dir)
        valid_pairs = [(c, p) for c, p in zip(batch, local_paths) if p is not None]
        if not valid_pairs:
            continue
        batch = [c for c, _ in valid_pairs]
        local_paths = [p for _, p in valid_pairs]

        try:
            results = _verify_image_batch(narration_excerpt, batch, local_paths)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] فشل التحقق من دفعة الصور: %s", scene_id, e)
            results = []

        for r in results:
            idx = r.get("candidate_index")
            score = r.get("score", 0)
            if idx is None or not (0 <= idx < len(batch)):
                continue
            if score < MEDIA_IMAGE_RELEVANCE_ACCEPT_THRESHOLD:
                continue
            cand = batch[idx]
            skey = _source_key(cand)
            if skey in used_source_keys:
                continue
            used_source_keys.add(skey)
            accepted.append((score, local_paths[idx], cand))
            logger.info("[%s] خطة الصور: قُبلت صورة (score=%.1f) من %s.", scene_id, score, cand.get("source"))
            if len(accepted) >= MAX_IMAGES_PER_SCENE:
                break

    if len(accepted) < MIN_IMAGES_PER_SCENE:
        logger.info(
            "[%s] خطة الصور البديلة: لم يُعثر إلا على %d/%d صورة بعتبة %.1f، تراجع لمنطق fallback القديم.",
            scene_id, len(accepted), MIN_IMAGES_PER_SCENE, MEDIA_IMAGE_RELEVANCE_ACCEPT_THRESHOLD,
        )
        return None

    accepted = sorted(accepted, key=lambda a: a[0], reverse=True)[:MAX_IMAGES_PER_SCENE]
    image_paths = [p for _, p, _ in accepted]
    chosen_candidates = [c for _, _, c in accepted]
    min_score = min(s for s, _, _ in accepted)

    if ENABLE_CROSS_SCENE_MEDIA_DEDUP:
        for c in chosen_candidates:
            run_state.mark_source_used(_source_key(c))

    attributions = [
        c.get("attribution_text") for c in chosen_candidates
        if c.get("requires_attribution") and c.get("attribution_text")
    ]

    result = {
        "clip_path": image_paths[0],  # للتوافق مع أي كود/لوغ يفترض وجود clip_path
        "start": 0, "end": 0,
        "score": min_score,
        "needs_manual_review": False,
        "requires_attribution": bool(attributions),
        "attribution_text": "; ".join(dict.fromkeys(attributions)) if attributions else None,
        "media_type": "image_sequence",
        "images": image_paths,
    }
    run_state.set_scene_result(scene_id, **result)
    logger.info(
        "[%s] خطة الصور البديلة نجحت: %d صور (أدنى score=%.1f) بدل الفيديو المفقود.",
        scene_id, len(image_paths), min_score,
    )
    return result


def _source_key(candidate: dict) -> str:
    """مفتاح فريد لكل فيديو مصدر (وليس لكل مرشح/مقطع زمني منه)، يُستخدم لمنع
    اختيار نفس الفيديو المصدر لأكثر من مشهد ضمن نفس الفيديو النهائي (إصلاح
    "تكرار المشهد"). لا علاقة له بالنطاق الزمني (start/end) المختار من الفيديو."""
    return f"{candidate.get('source')}:{candidate.get('id')}"


def _get_actual_video_orientation(path: str) -> str | None:
    """
    فحص فعلي لاتجاه الفيديو عبر ffprobe (وليس الاعتماد فقط على أبعاد
    الميتاداتا القادمة من كل مصدر، والتي قد تكون غائبة تمامًا مثل NASA أو
    غير دقيقة أحيانًا). يعيد "portrait"/"landscape"/"square"، أو None لو فشل
    الفحص لأي سبب (ffprobe غير متاح، ملف تالف...).
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", path,
    ]
    try:
        out = subprocess.run(
            cmd, check=True, timeout=20,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout.decode().strip()
        width_str, height_str = out.split("x")[:2]
        width, height = int(width_str), int(height_str)
    except Exception as e:  # noqa: BLE001
        logger.warning("فشل فحص اتجاه الفيديو (ffprobe) لـ %s: %s", path, e)
        return None

    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def _filter_by_actual_orientation(top3: list[dict], local_paths: list[str]) -> tuple[list[dict], list[str]]:
    """
    فحص اتجاه فعلي (ffprobe) لكل مرشح مُحمَّل، ويستبعد أي مرشح اتجاهه غير
    مسموح حسب config.ALLOWED_VIDEO_ORIENTATIONS (عمودي فقط حاليًا). هذا فحص
    نهائي موحّد عبر كل المصادر الخمسة (خصوصًا NASA التي لا توفر أبعادًا في
    ميتاداتا البحث أصلًا)، بعد فلاتر الاتجاه المبدئية في media_downloader
    التي تعتمد على أبعاد الميتاداتا المُعلَنة من كل مصدر.
    """
    # فحص ffprobe لكل مرشح مستقل تمامًا عن البقية (كل فحص يقرأ ملفه الخاص فقط)،
    # فتشغيلها بالتوازي عبر خيوط لا يغيّر أي نتيجة فحص، فقط يسرّع الوصول إليها
    # (خصوصًا أن ffprobe نفسه ينتظر IO أغلب وقته لا CPU).
    with ThreadPoolExecutor(max_workers=max(1, len(local_paths))) as ex:
        orientations = list(ex.map(_get_actual_video_orientation, local_paths))

    kept_candidates, kept_paths = [], []
    for cand, path, orientation in zip(top3, local_paths, orientations):
        # اتجاه غير معروف (فشل الفحص) -> نستبعد حذرًا، نفس منطق الحذر
        # المطبّق على الرخص/الدقة غير الواضحة في بقية النظام.
        if orientation is None or orientation not in ALLOWED_VIDEO_ORIENTATIONS:
            logger.info(
                "[تصفية اتجاه] استُبعد مرشح %s/%s (اتجاه=%s، المسموح=%s).",
                cand.get("source"), cand.get("id"), orientation, ALLOWED_VIDEO_ORIENTATIONS,
            )
            continue
        kept_candidates.append(cand)
        kept_paths.append(path)
    return kept_candidates, kept_paths


def _format_candidates_meta(candidates: list[dict]) -> str:
    """يبني كتلة نصية بعنوان/وصف كل مرشح (إن توفّرا) لإرسالها مع الفيديو نفسه
    لـ Gemini، بدل الاعتماد على التحليل البصري وحده (نقطة الطلب: تمرير
    الميتاداتا مع الفيديو عند التحليل)."""
    lines = []
    for i, c in enumerate(candidates):
        title = c.get("title") or "(بدون عنوان)"
        description = c.get("description") or "(بدون وصف)"
        lines.append(f"- الفيديو {i} [{c.get('source')}]: العنوان: {title} | الوصف: {description}")
    return "\n".join(lines)


def _verify_batch(narration_excerpt: str, candidates: list[dict], local_paths: list[str]) -> list[dict]:
    prompt = _VERIFY_PROMPT.format(
        n=len(candidates),
        narration_excerpt=narration_excerpt,
        candidates_meta=_format_candidates_meta(candidates),
    )
    raw = call_gemini_with_rotation(
        STAGE, [prompt], media_paths=local_paths, response_mime_type="application/json"
    )
    return parse_json_response(raw).get("results", [])


def _request_alternative_keywords(
    narration_excerpt: str, tried_keywords: list[str], video_topic: str = ""
) -> list[str]:
    prompt = _ALT_KEYWORD_PROMPT.format(
        video_topic=video_topic or "غير محدد",
        narration_excerpt=narration_excerpt,
        tried_keywords="، ".join(tried_keywords),
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


def verify_scene_media(scene: dict, run_state: RunState, media_dir: str, category: str = "", topic: str = "") -> dict | None:
    """
    يشغّل حلقة كاملة للمشهد: بحث -> تحميل أول 3 -> تحقق دفعة واحدة -> قبول/رفض ->
    تبديل كلمة مفتاحية عند الحاجة. يعيد dict {clip_path, start, end, score} أو None عند الفشل التام.
    category/topic: تُمرَّران لمصادر الوسائط لتفعيل مصدر ناسا تلقائيًا للمحتوى
    المتعلق بالفضاء/الفلك، بفحص الفئة والموضوع وكلمات المشهد معًا وليس الفئة
    وحدها (نقطة 2 و4).
    """
    scene_id = scene["id"]
    narration_excerpt = scene["text"]
    keywords_pool = list(scene.get("visual_keywords", []))

    # أفضل مرشح رُصد عبر كل الجولات (حتى لو لم يبلغ عتبة القبول)، يُستخدم كخطة
    # احتياطية بدل إفشال المشهد/الفيديو بالكامل إن لم يصل أي مرشح للعتبة.
    fallback_best = None  # (score, clip_path, start, end)

    for retry in range(MAX_KEYWORD_RETRY_PER_SCENE):
        remaining_keywords = [
            kw for kw in keywords_pool if not run_state.keyword_already_tried(scene_id, kw)
        ]
        if not remaining_keywords:
            logger.warning("[%s] لا كلمات مفتاحية متبقية، طلب بدائل من Gemini.", scene_id)
            tried = run_state.data["used_keywords"].get(scene_id, [])
            video_topic = run_state.data.get("video_title", "")
            remaining_keywords = _request_alternative_keywords(narration_excerpt, tried, video_topic)
            keywords_pool.extend(remaining_keywords)

        candidates = search_scene_media_sync(remaining_keywords, category=category, topic=topic)
        if not candidates:
            logger.warning("[%s] لم يُعثر على أي مرشح لهذه الجولة.", scene_id)
            for kw in remaining_keywords:
                run_state.mark_keyword_used(scene_id, kw)
            continue

        # نستبعد أي مرشح يعود لنفس الفيديو المصدر الذي استُخدم فعلاً في مشهد
        # آخر مقبول ضمن هذا الفيديو، لمنع تكرار نفس اللقطة في أكثر من مشهد.
        if ENABLE_CROSS_SCENE_MEDIA_DEDUP:
            before_dedup = len(candidates)
            candidates = [c for c in candidates if not run_state.source_already_used(_source_key(c))]
            removed = before_dedup - len(candidates)
            if removed:
                logger.info(
                    "[%s] استُبعد %d مرشح لأن مصدره مستخدَم فعلاً في مشهد آخر من نفس الفيديو.",
                    scene_id, removed,
                )
            if not candidates:
                logger.warning("[%s] كل مرشحي هذه الجولة مستبعدون (مصادرهم مستخدَمة في مشاهد أخرى).", scene_id)
                for kw in remaining_keywords:
                    run_state.mark_keyword_used(scene_id, kw)
                continue

        candidates = _interleave_candidates(candidates)
        top3 = candidates[:MAX_CANDIDATES_PER_VERIFICATION_BATCH]

        async def _download_all(cands):
            # تشغيل كل التحميلات معًا عبر gather بدل انتظار كل واحد قبل بدء
            # التالي (كما كان سابقًا رغم استخدام async/await، كانت التحميلات
            # فعليًا تسلسلية لأن كل await ينتظر قبل بدء التالي). download_candidate
            # نفسها async فعليًا (aiohttp)، فالتوازي هنا حقيقي ولا يغيّر أي شيء
            # في نتيجة التحميل نفسها أو إعادة المحاولة الداخلية لكل ملف.
            results = await asyncio.gather(
                *[download_candidate(c, media_dir) for c in cands],
                return_exceptions=True,
            )
            paths = []
            for c, r in zip(cands, results):
                if isinstance(r, Exception):
                    logger.warning("فشل تحميل %s: %s", c.get("url"), r)
                    paths.append(None)
                else:
                    paths.append(r)
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

        # فلتر الاتجاه النهائي الفعلي (ffprobe) عبر كل المصادر، بعد التحميل
        # مباشرة وقبل توليد المعاينات/إرسالها لـGemini - يستبعد أي مرشح ليس
        # عموديًا حاليًا (أو ليس ضمن config.ALLOWED_VIDEO_ORIENTATIONS).
        top3, local_paths = _filter_by_actual_orientation(top3, local_paths)
        if not top3:
            logger.warning("[%s] كل مرشحي هذه الجولة استُبعدوا بفلتر الاتجاه.", scene_id)
            for kw in {c["keyword"] for c in candidates[:MAX_CANDIDATES_PER_VERIFICATION_BATCH]}:
                run_state.mark_keyword_used(scene_id, kw)
            continue

        # نولّد معاينة مضغوطة من كل فيديو لإرسالها لـ Gemini (بدل الملف الأصلي
        # الذي غالبًا يتجاوز حد الـ inline data)، مع الإبقاء على local_paths
        # الأصلية كاملة الجودة لاستخدامها لاحقًا في المونتاج النهائي.
        # توليد المعاينات المضغوطة بالتوازي (كل معاينة عملية ffmpeg مستقلة عن
        # الأخرى بملف مختلف)؛ نفس أمر ffmpeg ونفس جودة/مدة المعاينة بالضبط،
        # فقط تُنفَّذ كل العمليات في نفس الوقت بدل التتابع.
        with ThreadPoolExecutor(max_workers=max(1, len(local_paths))) as ex:
            raw_previews = list(ex.map(lambda p: _make_verification_preview(p, media_dir), local_paths))
        preview_paths = [rp or p for rp, p in zip(raw_previews, local_paths)]

        try:
            results = _verify_batch(narration_excerpt, top3, preview_paths)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] فشل التحقق من الدفعة: %s", scene_id, e)
            results = []

        accepted = [r for r in results if r.get("score", 0) >= MEDIA_RELEVANCE_ACCEPT_THRESHOLD]
        for kw in {c["keyword"] for c in top3}:
            run_state.mark_keyword_used(scene_id, kw)

        # نحدّث أفضل مرشح احتياطي عبر كل النتائج (وليس فقط المقبولين)، للاستخدام
        # لاحقًا إن فشلت كل الجولات في بلوغ العتبة المطلوبة.
        for r in sorted(results, key=lambda r: r.get("score", 0), reverse=True):
            r_segment = r.get("best_segment")
            if (
                r.get("candidate_index") is None
                or not (0 <= r["candidate_index"] < len(top3))
                or local_paths[r["candidate_index"]] is None
                or not r_segment
            ):
                continue
            r_score = r.get("score", 0)
            if fallback_best is None or r_score > fallback_best[0]:
                fallback_best = (
                    r_score, local_paths[r["candidate_index"]],
                    r_segment["start"], r_segment["end"],
                    top3[r["candidate_index"]],
                )
            break

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
                "needs_manual_review": False,
                "requires_attribution": chosen_candidate.get("requires_attribution", False),
                "attribution_text": chosen_candidate.get("attribution_text"),
                "media_type": "video",
            }
            run_state.set_scene_result(scene_id, **result)
            if ENABLE_CROSS_SCENE_MEDIA_DEDUP:
                run_state.mark_source_used(_source_key(chosen_candidate))
            logger.info("[%s] قُبل بعد %d محاولة (score=%.1f).", scene_id, retry + 1, best["score"])
            return result

        logger.info("[%s] رُفضت كل المرشحين في هذه الجولة (محاولة %d/%d).",
                    scene_id, retry + 1, MAX_KEYWORD_RETRY_PER_SCENE)

    # قبل اللجوء لأفضل مرشح فيديو ضعيف (fallback القديم) أو المراجعة اليدوية،
    # نجرّب خطة الصور البديلة الصارمة: قد تتوفر صور مطابقة بدقة عالية جدًا
    # (>= MEDIA_IMAGE_RELEVANCE_ACCEPT_THRESHOLD) حتى لو فشل إيجاد فيديو مناسب.
    # لو نجحت، نعتبر المشهد ناجحًا تمامًا (لا حاجة لمراجعة يدوية) ونتخطى منطق
    # fallback الفيديو القديم بالكامل. لو فشلت (لم تكفِ 3 صور بهذه العتبة
    # الصارمة)، نتابع بنفس المنطق القديم كما كان تمامًا دون أي تغيير.
    logger.info("[%s] استُنفدت محاولات الفيديو دون بلوغ العتبة، تجربة خطة الصور البديلة الصارمة...", scene_id)
    image_result = _try_image_fallback(scene, run_state, media_dir, category=category, topic=topic)
    if image_result is not None:
        return image_result

    if fallback_best is not None:
        score, clip_path, start, end, fb_candidate = fallback_best

        # نقطة 3 (ج): لا نقبل "أفضل مرشح مهما كانت درجته" حتى في وضع fallback؛
        # نفرض حدًا أدنى مطلقًا (MIN_FALLBACK_ACCEPT_SCORE). إن لم يتحقق، يُعلَّم
        # المشهد بعلم needs_manual_review بدل المتابعة الصامتة.
        needs_manual_review = score < MIN_FALLBACK_ACCEPT_SCORE
        result = {
            "clip_path": clip_path,
            "start": start,
            "end": end,
            "score": score,
            "needs_manual_review": needs_manual_review,
            "requires_attribution": fb_candidate.get("requires_attribution", False),
            "attribution_text": fb_candidate.get("attribution_text"),
            "media_type": "video",
        }
        run_state.set_scene_result(scene_id, **result)
        if ENABLE_CROSS_SCENE_MEDIA_DEDUP:
            run_state.mark_source_used(_source_key(fb_candidate))
        if needs_manual_review:
            logger.error(
                "[%s] أفضل مرشح احتياطي (score=%.1f) أقل من الحد الأدنى المطلق "
                "لـ fallback (%.1f)؛ تُعلَّم هذه المشهد needs_manual_review بدل "
                "قبوله تلقائيًا.",
                scene_id, score, MIN_FALLBACK_ACCEPT_SCORE,
            )
        else:
            logger.warning(
                "[%s] لم يصل أي مرشح لعتبة القبول (%.1f) بعد %d محاولات؛ استُخدم أفضل مرشح "
                "متاح كخطة احتياطية (score=%.1f، ضمن الحد الأدنى المطلق %.1f) بدل إفشال "
                "الفيديو بالكامل.",
                scene_id, MEDIA_RELEVANCE_ACCEPT_THRESHOLD, MAX_KEYWORD_RETRY_PER_SCENE,
                score, MIN_FALLBACK_ACCEPT_SCORE,
            )
        return result

    logger.error("[%s] فشل نهائي بعد %d محاولات.", scene_id, MAX_KEYWORD_RETRY_PER_SCENE)
    return None
