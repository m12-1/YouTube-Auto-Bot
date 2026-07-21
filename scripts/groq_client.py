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
import time

from scripts import config
from scripts.retry_utils import RateLimiter
from scripts.telegram_alerts import alert_key_error

# النموذج الوحيد متعدد الوسائط المتاح حالياً على Groq (راجع console.groq.com/docs/vision)
MODEL_VISION = "qwen/qwen3.6-27b"
MAX_IMAGES_PER_REQUEST = 5      # حد Groq API الرسمي بعدد الصور بالطلب الواحد
FRAME_INTERVAL_SECONDS = 5      # استخراج إطار كل 5 ثوانٍ من الفيديو (طلب المستخدم)
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")

# نفس فكرة gemini_client: 15 ثانية على الأقل بين طلب وآخر لطبقة Groq،
# لأنها الآن تعمل بالتوازي مع Gemini على عناصر مختلفة بنفس الوقت.
_VERIFY_RATE_LIMITER = RateLimiter(min_interval=15.0)

RUBRIC_PROMPT_TEMPLATE = """You are a strict visual quality inspector for a YouTube Short. Score how well this media (image, or frames sampled from a video clip) matches the narration below, using the 5 criteria. Reply with JSON ONLY, no extra text.

Narration: "{narration}"

Criteria:
1. semantic_match (0-3): exact semantic match to the narration's meaning. 3 = precise match, 1-2 = related but generic/shallow, 0 = unrelated.
2. framing (0-2): suitability for vertical 9:16 cropping. 2 = vertical or center-framed subject, 1 = subject moves a lot and may leave frame, 0 = horizontal with subject at the edges.
3. quality (0-2): visual quality. 2 = excellent lighting/high resolution, 1 = acceptable but dull colors/weak lighting, 0 = low resolution or jarring colors.
4. motion (0-2): motion dynamics (if multiple frames are shown, judge the clip as a whole; if this is a single still image, use 1). 2 = clear cinematic motion, 1 = very slow/almost static, 0 = shaky or chaotic motion.
5. cleanliness (0-1): free of embedded text/watermarks/people talking to camera. 1 = clean, 0 = has burned-in text or watermarks.

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


def _build_prompt(narration: str, num_images: int) -> str:
    multi_frame_note = (
        f"You are shown {num_images} frames sampled every {FRAME_INTERVAL_SECONDS} seconds "
        "across the SAME video clip, in chronological order. Judge whether the clip AS A WHOLE "
        "matches the narration — approve if the overall subject matches even if one single frame "
        "is a transition or slightly ambiguous. "
    ) if num_images > 1 else ""

    return (
        "You are a strict visual quality inspector for a YouTube video. "
        "Does this media clearly and literally show exactly what is described in the narration? "
        + multi_frame_note +
        "If the narration mentions 'video games', 'digital graphics', or 'pixels', and the image shows a physical board game (like chess or foosball), you MUST answer NO. "
        "If the image is completely unrelated to the core subject of the narration, answer NO. "
        "Answer ONLY with YES or NO.\n\n"
        f"Narration: {narration}"
    )


def _call_groq_vision(image_paths: list[str], narration: str) -> bool:
    client = _get_client()
    content = [{"type": "text", "text": _build_prompt(narration, len(image_paths))}]
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
            if attempt == 0 and ("429" in error_str or "503" in error_str or "rate" in error_str):
                print(f"[GROQ] ازدحام/حصة ({e}). محاولة أخيرة بعد 3 ثوانٍ...")
                time.sleep(3)
                continue
            break

    # تنبيه تليقرام فقط لو الخطأ فعلاً مصادقة/مفتاح غير صالح — وليس أي خطأ آخر
    # (حصة، ازدحام سيرفر، فشل JSON عابر...الخ) لتفادي رسائل "المفتاح معطّل" المضلِّلة
    last_error_str = str(last_error).lower()
    is_real_key_error = any(
        s in last_error_str for s in ("401", "403", "invalid api key", "authentication", "unauthorized")
    )
    if is_real_key_error:
        alert_key_error("Groq", "GROQ_API_KEY", str(last_error))
    raise GroqVerificationUnavailable(str(last_error))


def verify_media_relevance(file_path: str, narration: str) -> bool:
    """
    الدالة الرئيسية لهذه الطبقة — نفس توقيع gemini_client.verify_media_relevance
    عمداً، لتُستخدم بنفس النمط داخل media_relevance_checker.py.

    مهم: على عكس طبقتي Gemini و CLIP اللتين تستقبلان صورة واحدة مسبقة
    الاستخراج فقط، هذه الدالة تستقبل مسار الملف الأصلي (قد يكون فيديو) وتتولى
    بنفسها استخراج عدة إطارات كل 5 ثوانٍ عند الحاجة.

    ترفع GroqVerificationUnavailable لو تعذر الوصول لـ Groq لأي سبب — القرار
    النهائي (تفعيل CLIP أو الرفض) يبقى مسؤولية media_relevance_checker.py.
    """
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

        is_relevant = _call_groq_vision(image_paths, narration)
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


def _call_groq_score(image_paths: list[str], narration: str) -> dict:
    client = _get_client()
    content = [{"type": "text", "text": RUBRIC_PROMPT_TEMPLATE.format(narration=narration)}]
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
            return {"score": score, "passed": score > 7, "breakdown": breakdown, "layer": "groq", "model": MODEL_VISION}
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_quota = "429" in error_str or "503" in error_str or "rate" in error_str
            is_json_glitch = "json_validate_failed" in error_str or "failed to validate json" in error_str
            if attempt < 2 and (is_quota or is_json_glitch):
                wait_s = 3 if is_quota else 1.5
                print(f"[GROQ] {'ازدحام/حصة' if is_quota else 'فشل توليد JSON صالح (عطل عابر بالنموذج، ليس بالمفتاح)'} ({e}). إعادة المحاولة بعد {wait_s} ثانية...")
                time.sleep(wait_s)
                continue
            break

    # لا نُرسل تنبيه "المفتاح معطّل" إلا لو كان الخطأ فعلاً مصادقة/مفتاح غير
    # صالح — أخطاء الحصة وأخطاء JSON العابرة (بعد استنفاد المحاولات) لا تعني
    # عطل المفتاح نفسه، فقط أن هذه الطبقة الاحتياطية تعذّر الوصول لها الآن.
    last_error_str = str(last_error).lower()
    is_real_key_error = any(
        s in last_error_str for s in ("401", "403", "invalid api key", "authentication", "unauthorized")
    )
    if is_real_key_error:
        alert_key_error("Groq", "GROQ_API_KEY", str(last_error))
    raise GroqVerificationUnavailable(str(last_error))



def score_media_relevance(file_path: str, narration: str) -> dict:
    """نظام تقييم من 10 عبر Groq (نفس معايير gemini_client.score_media_relevance)،
    مع فاصل 15 ثانية إلزامي بين الطلبات لأن هذه الطبقة تعمل الآن بالتوازي
    مع Gemini على عناصر مختلفة بنفس الوقت. تدعم فيديو (تستخرج عدة إطارات)
    وصورة (إطار واحد)."""
    is_video = file_path.lower().endswith(VIDEO_EXTENSIONS)
    frame_paths: list[str] = []
    _get_client()

    try:
        if is_video:
            frame_paths = _extract_frames(file_path)
            image_paths = frame_paths
        else:
            image_paths = [file_path]

        return _call_groq_score(image_paths, narration)
    finally:
        if frame_paths:
            _cleanup_frames(frame_paths)
