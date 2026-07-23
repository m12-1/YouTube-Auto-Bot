"""
groq_client.py
طبقة تحليل بصري ثانية (احتياطية) — تعمل بين Gemini (الطبقة الأولى) و CLIP
(الطبقة الأخيرة المحلية)، باستخدام Groq API ونموذج qwen/qwen3.6-27b متعدد
الوسائط (نص + صورة)، وهو النموذج الوحيد المتاح حالياً على Groq لفهم الصور.

لماذا هذه الطبقة موجودة؟
-------------------------
لو فشلت كل نماذج Gemini الأربعة (نفاد حصة 429 / ازدحام سيرفر 503 / انقطاع
اتصال)، بدل القفز مباشرة لحارس CLIP المحلي (نموذج تشابه أضعف دلالياً من LLM
حقيقي يفهم اللغة)، نجرب أولاً Groq — منصة استدلال فائقة السرعة توفر حكماً
لغوياً/بصرياً أقرب لجودة Gemini، قبل اللجوء لـ CLIP كخط دفاع أخير.

ملاحظة مهمة عن الفيديو:
------------------------
نموذج Groq الحالي (qwen/qwen3.6-27b) لا يقبل ملفات فيديو مباشرة — يقبل صوراً
فقط (بحد أقصى 5 صور بالطلب الواحد وفق توثيق Groq الرسمي). لذلك لو كان الوسيط
فيديو، نستخرج إطارات منه برمجياً كل 5 ثوانٍ (حتى 5 إطارات كحد أقصى) عبر
ffmpeg، ونرسلها جميعاً بنفس الطلب، ونطلب من النموذج الحكم على تطابق المقطع
كاملاً مع النص السردي، وليس لحظة واحدة فقط كما يفعل الفحص الأصلي بـ Gemini/CLIP.
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time

from scripts import config, content_policy
from scripts.retry_utils import RateLimiter
from scripts.telegram_alerts import alert_key_error

# نفس قائمة المحظورات المستخدمة بـ gemini_client (مصدر واحد: content_policy)
_GLOBAL_NEGATIVE_KEYWORDS = ", ".join(sorted(set(content_policy.all_blocked_keywords_flat())))


def _mood_and_negative_clause(topic_context: str) -> str:
    mood = (topic_context or "").strip()
    mood_clause = (
        f'The overall visual identity/mood of this whole video is: "{mood}". Judge this scene '
        f"WITHIN that context — reject it if it looks out of place for that mood/identity even if it "
        f"superficially matches a single word in the narration. "
        if mood else ""
    )
    negative_clause = (
        f"You MUST reject (answer NO / semantic_match = 0) if the media shows any of: "
        f"{_GLOBAL_NEGATIVE_KEYWORDS}, or anything visually unrelated to the video's topic/mood above. "
    )
    return mood_clause + negative_clause

# --- كاش استنفاد الحصة اليومية (TPD) بالذاكرة ---
# المشكلة الأصلية: خطأ TPD (tokens per day) يختلف عن TPM (تدقيقة) — TPM
# يتعافى خلال ثوانٍ/دقائق، أما TPD فلا يتعافى إلا بعد ساعات. بدون هذا الكاش
# كان الكود يستمر بإعادة محاولة Groq لكل مشهد جديد لبقية التشغيل رغم أن كل
# محاولة كانت مضمونة الفشل. الآن: أول ما نكتشف رسالة "tokens per day"
# (أو ما يعادلها)، نضع علامة "Groq غير متاح لبقية هذا التشغيل" في الذاكرة
# ونتخطاه فوراً بلا أي طلب شبكة لبقية المشاهد، بدل تكرار محاولات فاشلة مضمونة.
_daily_quota_lock = threading.Lock()
_daily_quota_exhausted = False


def is_daily_exhausted() -> bool:
    with _daily_quota_lock:
        return _daily_quota_exhausted


def _mark_daily_exhausted() -> None:
    global _daily_quota_exhausted
    with _daily_quota_lock:
        if not _daily_quota_exhausted:
            _daily_quota_exhausted = True
            print("[GROQ] اكتُشف تجاوز الحصة اليومية (TPD). سيتم تخطي Groq فوراً بلا أي محاولة شبكة لبقية هذا التشغيل.")


def _is_daily_quota_message(error_str: str) -> bool:
    """يميّز رسائل الحصة اليومية (TPD — لا تتعافى إلا بعد ساعات) عن رسائل
    حصة الدقيقة (TPM — تتعافى خلال ثوانٍ/دقائق). Groq يذكر هذا صراحة
    برسائل من نوع 'Limit ... tokens per day (TPD)'."""
    e = error_str.lower()
    return (
        "tokens per day" in e or "requests per day" in e or
        "(tpd)" in e or " tpd" in e or "(rpd)" in e or " rpd" in e
    )

# النموذج الوحيد متعدد الوسائط المتاح حالياً على Groq (راجع console.groq.com/docs/vision)
MODEL_VISION = "qwen/qwen3.6-27b"
MAX_IMAGES_PER_REQUEST = 3      # حد Groq الفعلي بعدد الصور بالطلب الواحد لهذا الموديل (qwen/qwen3.6-27b) —
                                 # كان مضبوطاً خطأً على 5 سابقاً، ما كان يسبب فشل كل مشهد فيديو بخطأ
                                 # "Too many images provided. This model supports up to 3 images" (400)
                                 # مباشرة من Groq نفسه — راجع سجل التشغيل.
FRAME_INTERVAL_SECONDS = 5      # استخراج إطار كل 5 ثوانٍ من الفيديو (طلب المستخدم)
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

# نفس فكرة gemini_client: 15 ثانية على الأقل بين طلب وآخر لطبقة Groq،
# لأنها الآن تعمل بالتوازي مع Gemini على عناصر مختلفة بنفس الوقت.
_VERIFY_RATE_LIMITER = RateLimiter(min_interval=15.0)

RUBRIC_PROMPT_TEMPLATE = """You are a strict visual quality inspector for a YouTube Short. Score how well this media (image, or frames sampled from a video clip) matches the narration below, using the 5 criteria. Reply with JSON ONLY, no extra text.

Narration/Search Query: "{narration}"

Criteria (Total 10, Semantic Match is most important):
1. semantic_match (0-6): exact semantic match to the narration's meaning. 6 = precise literal match, 3-5 = related but generic/shallow, 0-2 = unrelated or wrong metaphor. (If 2 or less, scene will be automatically rejected).
2. quality (0-1.5): visual quality. 1.5 = excellent lighting/high resolution, 0.5 = acceptable, 0 = low resolution/jarring colors.
3. framing (0-1): suitability for vertical 9:16 cropping. 1 = excellent, 0 = poor.
4. motion (0-1): motion dynamics. 1 = clear motion, 0 = almost static/shaky.
5. cleanliness (0-0.5): free of text/watermarks. 0.5 = clean, 0 = has text.

Reply with EXACTLY this JSON shape and nothing else:
{{"semantic_match": <number>, "framing": <number>, "quality": <number>, "motion": <number>, "cleanliness": <number>}}
"""


_client = None


class GroqVerificationUnavailable(Exception):
    """تُرفع عندما يتعذّر الحصول على قرار من Groq لأي سبب (مفتاح ناقص،
    429/503، انقطاع اتصال، فشل استخراج إطارات الفيديو...الخ) — تسمح لـ
    media_relevance_checker.py بالانتقال لحارس CLIP المحلي كطبقة حماية
    ثالثة وأخيرة، بدل الموافقة التلقائية العمياء."""
    pass


class GroqDailyQuotaExceeded(GroqVerificationUnavailable):
    """تُرفع تحديداً لو كان الخطأ تجاوز حصة يومية (TPD/RPD) وليس حصة دقيقة
    عابرة (TPM) — تسمح لـ analysis_engine.py بالتبديل الفوري والدائم لهذه
    الطبقة إلى Puter لبقية هذا التشغيل، بدل إعادة محاولات مضمونة الفشل."""
    pass


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not config.GROQ_API_KEY:
        raise GroqVerificationUnavailable("مفتاح GROQ_API_KEY غير موجود بالأسرار (GitHub Secrets)")
    from groq import Groq
    _client = Groq(api_key=config.GROQ_API_KEY)
    return _client


def _encode_image_data_url(image_path: str) -> str:
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{ext};base64,{b64}"


def _extract_frames(video_path: str) -> list[str]:
    """يستخرج إطاراً كل FRAME_INTERVAL_SECONDS ثوانٍ من الفيديو عبر ffmpeg،
    وحتى MAX_IMAGES_PER_REQUEST كحد أقصى (حد Groq بعدد الصور بالطلب الواحد).
    يرجع قائمة مسارات مؤقتة (يجب حذفها بعد الاستخدام من المستدعي)."""
    tmp_dir = tempfile.mkdtemp(prefix="groq_frames_")
    pattern = os.path.join(tmp_dir, "frame_%03d.jpg")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"fps=1/{FRAME_INTERVAL_SECONDS}",
            "-frames:v", str(MAX_IMAGES_PER_REQUEST),
            "-q:v", "5", pattern,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception as e:
        raise GroqVerificationUnavailable(f"فشل استخراج إطارات الفيديو عبر ffmpeg: {e}")

    frames = sorted(
        os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.lower().endswith(".jpg")
    )
    if not frames:
        raise GroqVerificationUnavailable("لم يُستخرج أي إطار من الفيديو (قد يكون الملف تالفاً أو قصيراً جداً)")
    return frames


def _cleanup_frames(frame_paths: list[str]) -> None:
    tmp_dirs = set()
    for f in frame_paths:
        tmp_dirs.add(os.path.dirname(f))
        try:
            os.remove(f)
        except Exception:
            pass
    for d in tmp_dirs:
        try:
            os.rmdir(d)
        except Exception:
            pass


def _build_prompt(narration: str, num_images: int, topic_context: str = "") -> str:
    multi_frame_note = (
        f"You are shown {num_images} frames sampled every {FRAME_INTERVAL_SECONDS} seconds "
        "across the SAME video clip, in chronological order. Judge whether the clip AS A WHOLE "
        "matches the narration — approve if the overall subject matches even if one single frame "
        "is a transition or slightly ambiguous. "
    ) if num_images > 1 else ""

    return (
        "You are a strict visual quality inspector for a YouTube video. "
        "Does this media clearly and literally show exactly what is described in the narration? "
        + multi_frame_note + _mood_and_negative_clause(topic_context) +
        "If the narration mentions 'video games', 'digital graphics', or 'pixels', and the image shows a physical board game (like chess or foosball), you MUST answer NO. "
        "If the image is completely unrelated to the core subject of the narration, answer NO. "
        "Answer ONLY with YES or NO.\n\n"
        f"Narration: {narration}"
    )


def _call_groq_vision(image_paths: list[str], narration: str, topic_context: str = "") -> bool:
    client = _get_client()
    content = [{"type": "text", "text": _build_prompt(narration, len(image_paths), topic_context)}]
    for img_path in image_paths:
        content.append({
            "type": "image_url",
            "image_url": {"url": _encode_image_data_url(img_path)},
        })

    last_error = None
    for attempt in range(2):  # محاولة واحدة إضافية فقط عند 429/503 — هذه طبقة احتياطية سريعة، لا نريد تأخير الإنتاج
        try:
            completion = client.chat.completions.create(
                model=MODEL_VISION,
                messages=[{"role": "user", "content": content}],
                temperature=0.0,
                max_completion_tokens=10,
            )
            text = (completion.choices[0].message.content or "").strip().upper()
            return "YES" in text
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            if _is_daily_quota_message(error_str):
                # حصة يومية (TPD) — لا فائدة من إعادة المحاولة، الحصة لن تعود إلا بعد ساعات
                print(f"[GROQ] تجاوز حصة يومية (TPD) ({e}). لا داعي لإعادة المحاولة.")
                break
            if attempt == 0 and ("429" in error_str or "503" in error_str or "rate" in error_str):
                print(f"[GROQ] ازدحام/حصة دقيقة (TPM) ({e}). محاولة أخيرة بعد 3 ثوانٍ...")
                time.sleep(3)
                continue
            break

    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        _mark_daily_exhausted()
        raise GroqDailyQuotaExceeded(str(last_error))

    # تنبيه تليقرام فقط لو الخطأ فعلاً مصادقة/مفتاح غير صالح — وليس أي خطأ آخر
    # (حصة، ازدحام سيرفر، فشل JSON عابر...الخ) لتفادي رسائل "المفتاح معطّل" المضلِّلة
    is_real_key_error = any(
        s in last_error_str for s in ("401", "403", "invalid api key", "authentication", "unauthorized")
    )
    if is_real_key_error:
        alert_key_error("Groq", "GROQ_API_KEY", str(last_error))
    raise GroqVerificationUnavailable(str(last_error))


def verify_media_relevance(file_path: str, narration: str, topic_context: str = "") -> bool:
    """
    الدالة الرئيسية لهذه الطبقة — نفس توقيع gemini_client.verify_media_relevance
    عمداً، لتُستخدم بنفس النمط داخل media_relevance_checker.py.

    مهم: على عكس طبقتي Gemini و CLIP اللتين تستقبلان صورة واحدة مسبقة
    الاستخراج فقط، هذه الدالة تستقبل مسار الملف الأصلي (قد يكون فيديو) وتتولى
    بنفسها استخراج عدة إطارات كل 5 ثوانٍ عند الحاجة.

    ترفع GroqVerificationUnavailable لو تعذر الوصول لـ Groq لأي سبب — القرار
    النهائي (تفعيل CLIP أو الرفض) يبقى مسؤولية media_relevance_checker.py.
    ترفع GroqDailyQuotaExceeded فوراً بلا أي طلب شبكة لو سبق أن اكتُشف
    استنفاد الحصة اليومية (TPD) بهذا التشغيل.
    """
    if is_daily_exhausted():
        raise GroqDailyQuotaExceeded("تم استنفاد حصة Groq اليومية (TPD) سابقاً بهذا التشغيل")

    is_video = file_path.lower().endswith(VIDEO_EXTENSIONS)
    frame_paths: list[str] = []

    # نتحقق من توفر المفتاح أولاً لتفادي استخراج إطارات فيديو بلا داعٍ لو
    # كان مفتاح GROQ_API_KEY غير موجود أصلاً.
    _get_client()

    try:
        if is_video:
            frame_paths = _extract_frames(file_path)
            image_paths = frame_paths
        else:
            image_paths = [file_path]

        is_relevant = _call_groq_vision(image_paths, narration, topic_context)
        return is_relevant
    finally:
        if frame_paths:
            _cleanup_frames(frame_paths)


def _parse_rubric_json(text: str) -> dict:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"Groq لم يُرجع JSON صالحاً: {text[:200]}")
    data = json.loads(match.group(0))
    return {
        "semantic_match": float(data.get("semantic_match", 0)),
        "framing": float(data.get("framing", 0)),
        "quality": float(data.get("quality", 0)),
        "motion": float(data.get("motion", 0)),
        "cleanliness": float(data.get("cleanliness", 0)),
    }


def _call_groq_score(image_paths: list[str], narration: str, topic_context: str = "") -> dict:
    client = _get_client()
    rubric_text = RUBRIC_PROMPT_TEMPLATE.format(narration=narration) + "\n" + _mood_and_negative_clause(topic_context)
    content = [{"type": "text", "text": rubric_text}]
    for img_path in image_paths:
        content.append({
            "type": "image_url",
            "image_url": {"url": _encode_image_data_url(img_path)},
        })

    last_error = None
    # 3 محاولات: json_validate_failed خطأ معروف من Groq نفسه (النموذج يفشل أحياناً
    # بتوليد JSON مطابق للصيغة تماماً، خصوصاً أنه نموذج preview) — ليس عطل مفتاح،
    # ويُحل غالباً بإعادة المحاولة مع رفع temperature قليلاً بدل الاستسلام فوراً.
    for attempt in range(3):
        _VERIFY_RATE_LIMITER.wait()
        try:
            completion = client.chat.completions.create(
                model=MODEL_VISION,
                messages=[{"role": "user", "content": content}],
                temperature=0.0 if attempt == 0 else 0.4,
                max_completion_tokens=200,
                response_format={"type": "json_object"},
            )
            text = completion.choices[0].message.content or ""
            breakdown = _parse_rubric_json(text)
            score = round(sum(breakdown.values()), 2)
            passed = score >= 7 and breakdown.get("semantic_match", 0) >= 4
            if not passed and score >= 7:
                print(f"[GROQ STRICT REJECT] تم رفض الفيديو رغم درجة ({score}) لأن التطابق الدلالي ضعيف جداً ({breakdown.get('semantic_match')}).")
                score = 0.0
                
            return {"score": score, "passed": passed, "breakdown": breakdown, "layer": "groq", "model": MODEL_VISION}
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            if _is_daily_quota_message(error_str):
                # حصة يومية (TPD) — لا فائدة من إعادة المحاولة، الحصة لن تعود إلا بعد ساعات
                print(f"[GROQ] تجاوز حصة يومية (TPD) ({e}). لا داعي لإعادة المحاولة.")
                break
            is_quota = "429" in error_str or "503" in error_str or "rate" in error_str
            is_json_glitch = "json_validate_failed" in error_str or "failed to validate json" in error_str
            if attempt < 2 and (is_quota or is_json_glitch):
                wait_s = 3 if is_quota else 1.5
                print(f"[GROQ] {'ازدحام/حصة دقيقة (TPM)' if is_quota else 'فشل توليد JSON صالح (عطل عابر بالنموذج، ليس بالمفتاح)'} ({e}). إعادة المحاولة بعد {wait_s} ثانية...")
                time.sleep(wait_s)
                continue
            break

    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        _mark_daily_exhausted()
        raise GroqDailyQuotaExceeded(str(last_error))

    # لا نُرسل تنبيه "المفتاح معطّل" إلا لو كان الخطأ فعلاً مصادقة/مفتاح غير
    # صالح — أخطاء الحصة وأخطاء JSON العابرة (بعد استنفاد المحاولات) لا تعني
    # عطل المفتاح نفسه، فقط أن هذه الطبقة الاحتياطية تعذّر الوصول لها الآن.
    is_real_key_error = any(
        s in last_error_str for s in ("401", "403", "invalid api key", "authentication", "unauthorized")
    )
    if is_real_key_error:
        alert_key_error("Groq", "GROQ_API_KEY", str(last_error))
    raise GroqVerificationUnavailable(str(last_error))



def score_media_relevance(file_path: str, narration: str, topic_context: str = "") -> dict:
    """نظام تقييم من 10 عبر Groq (نفس معايير gemini_client.score_media_relevance)،
    مع فاصل 15 ثانية إلزامي بين الطلبات لأن هذه الطبقة تعمل الآن بالتوازي
    مع Gemini على عناصر مختلفة بنفس الوقت. تدعم فيديو (تستخرج عدة إطارات)
    وصورة (إطار واحد).

    ترفع GroqDailyQuotaExceeded فوراً بلا أي طلب شبكة لو سبق أن اكتُشف
    استنفاد الحصة اليومية (TPD) بهذا التشغيل — هذا هو الإصلاح الذي يمنع
    تكرار محاولات فاشلة مضمونة على كل مشهد جديد لبقية التشغيل."""
    if is_daily_exhausted():
        raise GroqDailyQuotaExceeded("تم استنفاد حصة Groq اليومية (TPD) سابقاً بهذا التشغيل")

    is_video = file_path.lower().endswith(VIDEO_EXTENSIONS)
    frame_paths: list[str] = []
    _get_client()

    try:
        if is_video:
            frame_paths = _extract_frames(file_path)
            image_paths = frame_paths
        else:
            image_paths = [file_path]

        return _call_groq_score(image_paths, narration, topic_context)
    finally:
        if frame_paths:
            _cleanup_frames(frame_paths)
