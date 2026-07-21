"""
analysis_engine.py
محرك التحليل البصري الموحّد — ملف واحد يُستدعى عند الحاجة لفحص تطابق
أي وسيط (صورة/فيديو) مع نص سردي.

التحديث: الطبقتان الأوليان (Gemini و Groq) تعملان الآن بالتوازي بنفس
الوقت (بدل التسلسل) عبر ThreadPoolExecutor — كل واحدة تُصدر تقييماً من 10
حسب نظام النقاط الخماسي (راجع RUBRIC_PROMPT_TEMPLATE بكل من gemini_client
و groq_client): التطابق الدلالي (3) + التأطير (2) + الجودة (2) +
الحركة (2) + النظافة (1) = 10. الوسيط يُعتمد لو مجموع النقاط > 7.

كل طبقة (Gemini/Groq) تفرض بنفسها فاصلاً 15 ثانية على الأقل بين طلباتها
المتتالية (عبر RateLimiter بالملف الخاص بها) حتى لا تُستنفد الحصة
المجانية رغم التوازي.

سلسلة التدرّج الكاملة (Cascade) من 5 طبقات، من الأفضل للأسوأ:

  الطبقة 1+2 (بالتوازي): Gemini Vision (4 نماذج بالتسلسل) و
                          Groq (qwen/qwen3.6-27b) — تقييم من 10 لكل منهما.
                          نُفضّل نتيجة Gemini لو نجحت (طبقة 1 أصلاً)، وإلا
                          نأخذ نتيجة Groq لو نجحت هي.
  الطبقة 3: Puter AI (google/gemini-3.5-flash عبر بنيتهم — حصة منفصلة ثالثة)
  الطبقة 4: CLIP المحلي (بدون إنترنت — نموذج تشابه دلالي محلي)
  الطبقة 5: قبول تلقائي (فقط لو تعطل كل شيء — لمنع توقف الإنتاج)

قواعد الانتقال بين الطبقات:
- لو انتهت الحصة المجانية (429) أو توقف السيرفر (503) بكلا الطبقتين
  المتوازيتين → ينتقل فوراً لـ Puter
- لو أي خطأ آخر (مفتاح معطّل، شبكة...) → ينتقل + يرسل تنبيه تليقرام
- لو نجحت أي طبقة → يرجع القرار فوراً (لا يجرب الباقي)
"""
import concurrent.futures
import os
import subprocess

from scripts import gemini_client, groq_client
from scripts.telegram_alerts import send_alert

# عتبة القبول: أي وسيط مجموع نقاطه > 7 من 10 يُعتمد
PASS_THRESHOLD = 7

# --- CLIP configuration (same as was in media_relevance_checker.py) ---
CLIP_SIMILARITY_THRESHOLD = 0.20
CLIP_MODEL_NAME = "ViT-B-32"
CLIP_PRETRAINED = "openai"

_clip_model = None
_clip_preprocess = None
_clip_tokenizer = None


def _load_clip():
    """تحميل نموذج CLIP مرة واحدة فقط (Lazy Singleton)."""
    global _clip_model, _clip_preprocess, _clip_tokenizer
    if _clip_model is not None:
        return _clip_model, _clip_preprocess, _clip_tokenizer
    import open_clip
    print(f"[CLIP] تحميل نموذج {CLIP_MODEL_NAME} ({CLIP_PRETRAINED}) لأول مرة محلياً...")
    model, _, preprocess = open_clip.create_model_and_transforms(CLIP_MODEL_NAME, pretrained=CLIP_PRETRAINED)
    tokenizer = open_clip.get_tokenizer(CLIP_MODEL_NAME)
    model.eval()
    _clip_model, _clip_preprocess, _clip_tokenizer = model, preprocess, tokenizer
    return model, preprocess, tokenizer


def _clip_check(image_path: str, narration: str) -> bool:
    """فحص تطابق بصري عبر CLIP محلياً."""
    import torch
    from PIL import Image
    model, preprocess, tokenizer = _load_clip()
    text = (narration or "").strip()[:300] or "a relevant photo"
    with Image.open(image_path) as img:
        image_input = preprocess(img.convert("RGB")).unsqueeze(0)
    text_input = tokenizer([text])
    with torch.no_grad():
        image_features = model.encode_image(image_input)
        text_features = model.encode_text(text_input)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        similarity = (image_features @ text_features.T).item()
    print(f"[CLIP] درجة التشابه: {similarity:.3f} (عتبة القبول: {CLIP_SIMILARITY_THRESHOLD})")
    return similarity >= CLIP_SIMILARITY_THRESHOLD


def _extract_frame_for_check(file_path: str) -> tuple:
    """لو الملف فيديو، يستخرج إطاراً من الثانية 0.5 للطبقات التي تحتاج صورة.
    يرجع (مسار_الفحص, هل_هو_مؤقت)."""
    if not file_path.lower().endswith(('.mp4', '.webm', '.mov')):
        return file_path, False
    thumb_path = file_path + "_analysis_thumb.jpg"
    try:
        cmd = [
            "ffmpeg", "-y", "-i", file_path,
            "-ss", "00:00:00.500", "-vframes", "1",
            "-q:v", "5", thumb_path,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return thumb_path, True
    except Exception as e:
        print(f"[ANALYSIS] فشل استخراج إطار من الفيديو: {e}")
        return None, False


def _is_quota_error(error: Exception) -> bool:
    """يتحقق إذا كان الخطأ متعلقاً بالحصة (429/503/rate limit)."""
    err = str(error).lower()
    return "429" in err or "503" in err or "quota" in err or "rate" in err


def verify(file_path: str, narration: str) -> bool:
    """
    الدالة الرئيسية الوحيدة — تُستدعى من أي ملف يحتاج فحص تطابق بصري.

    تمرر الوسيط عبر سلسلة من 5 طبقات (من الأفضل للأسوأ) حتى تحصل
    على قرار (True/False). لو فشلت كل الطبقات → تقبل تلقائياً لمنع
    توقف الإنتاج.

    Args:
        file_path: مسار الصورة أو الفيديو
        narration: النص السردي المطلوب مطابقته

    Returns:
        bool: True إذا الوسيط يطابق النص، False إذا لا.
    """
    # استخراج إطار للطبقات التي تحتاج صورة (Gemini, CLIP)
    check_path, is_temp = _extract_frame_for_check(file_path)
    if check_path is None:
        print("[ANALYSIS] تعذر استخراج إطار من الفيديو. قبول تلقائي.")
        return True

    try:
        # ═══════════ الطبقتان 1+2 بالتوازي: Gemini + Groq (تقييم /10) ═══════════
        gemini_result, gemini_error = None, None
        groq_result, groq_error = None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            gemini_future = pool.submit(gemini_client.score_media_relevance, check_path, narration)
            groq_future = pool.submit(groq_client.score_media_relevance, file_path, narration)
            try:
                gemini_result = gemini_future.result()
            except Exception as e:
                gemini_error = e
            try:
                groq_result = groq_future.result()
            except Exception as e:
                groq_error = e

        if gemini_result is not None:
            print(f"[ANALYSIS L1] Gemini قيّم الوسيط بـ {gemini_result['score']}/10 "
                  f"({gemini_result['breakdown']}) لـ: {narration[:50]}...")
            return gemini_result["passed"]
        if gemini_error is not None:
            print(f"[ANALYSIS L1] Gemini غير متاح ({gemini_error}).")
            if not _is_quota_error(gemini_error) and not isinstance(gemini_error, gemini_client.GeminiVerificationUnavailable):
                from scripts.telegram_alerts import alert_key_error
                alert_key_error("Gemini Vision", "GEMINI_KEY_FILTER", str(gemini_error))

        if groq_result is not None:
            print(f"[ANALYSIS L2] Groq قيّم الوسيط بـ {groq_result['score']}/10 "
                  f"({groq_result['breakdown']}) لـ: {narration[:50]}...")
            return groq_result["passed"]
        if groq_error is not None:
            print(f"[ANALYSIS L2] Groq غير متاح ({groq_error}). الانتقال لـ Puter...")
            if not _is_quota_error(groq_error) and not isinstance(groq_error, groq_client.GroqVerificationUnavailable):
                from scripts.telegram_alerts import alert_key_error
                alert_key_error("Groq", "GROQ_API_KEY", str(groq_error))

        # كلتا الطبقتين المتوازيتين فشلتا → الانتقال لـ Puter
        # ═══════════════ الطبقة 3: Puter AI ═══════════════
        try:
            from scripts import puter_client
            result = puter_client.verify_media_relevance(file_path, narration)
            if not result:
                print(f"[ANALYSIS L3] Puter رفض الوسيط: {narration[:50]}...")
            return result
        except Exception as e:
            puter_err_name = type(e).__name__
            if not _is_quota_error(e):
                from scripts.telegram_alerts import alert_key_error
                alert_key_error("Puter AI", "PUTER_USERNAME", str(e))
            print(f"[ANALYSIS L3] Puter غير متاح ({puter_err_name}: {e}). الانتقال لـ CLIP...")

        # ═══════════════ الطبقة 4: CLIP المحلي ═══════════════
        try:
            result = _clip_check(check_path, narration)
            if not result:
                print(f"[ANALYSIS L4] CLIP رفض الوسيط: {narration[:50]}...")
            return result
        except Exception as e:
            print(f"[ANALYSIS L4] CLIP فشل أيضاً ({e}). قبول تلقائي كملاذ أخير.")

        # ═══════════════ الطبقة 5: قبول تلقائي ═══════════════
        send_alert(
            "⚠️ فشلت جميع طبقات التحليل البصري (Gemini → Groq → Puter → CLIP). "
            "تم قبول الوسيط تلقائياً لمنع توقف الإنتاج.",
            level="warning",
        )
        return True

    finally:
        if is_temp and check_path and os.path.exists(check_path):
            try:
                os.remove(check_path)
            except Exception:
                pass
