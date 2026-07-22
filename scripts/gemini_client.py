"""
gemini_client.py
مُحدث لعام 2026: يدعم موديلات Gemini 3 و Nano Banana الجديدة.
غلاف موحد فوق google-genai SDK.
"""
import json
import re
import threading

from google import genai
from scripts import config
from scripts.retry_utils import with_backoff, RateLimiter
from PIL import Image
from scripts.telegram_alerts import alert_key_error

_clients = {}

# يضمن 15 ثانية على الأقل بين طلب وآخر لطبقة التحقق البصري (تشغّل الآن
# بالتوازي مع Groq، فبدون هذا القفل يمكن أن تُستهلك الحصة المجانية أسرع
# بكثير من المسموح).
_VERIFY_RATE_LIMITER = RateLimiter(min_interval=15.0)

# --- كاش استنفاد الحصة اليومية (TPD/RPD) بالذاكرة ---
# 429/503 قد تكون "دقيقة" (تتعافى خلال ثوانٍ) أو "يومية" (لا تتعافى إلا بعد
# ساعات). لو اكتشفنا أن كل نماذج Gemini الأربعة فشلت برسالة تشير صراحة
# لحصة يومية، نضع علامة داخل الذاكرة ونتخطى Gemini فوراً بلا محاولة لبقية
# هذا التشغيل (بدل إعادة محاولات مضمونة الفشل على 4 نماذج في كل مشهد جديد).
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
            print("[GEMINI] اكتُشف تجاوز الحصة اليومية (TPD/RPD) على كل النماذج المتاحة. "
                  "سيتم تخطي Gemini فوراً بلا محاولة لبقية هذا التشغيل.")


def _is_daily_quota_message(error_str: str) -> bool:
    """يميّز رسائل الحصة اليومية (TPD/RPD — لا تتعافى إلا بعد ساعات) عن
    رسائل حصة الدقيقة/الثانية العابرة (429 عادي يتعافى خلال ثوانٍ)."""
    e = error_str.lower()
    return (
        "tokens per day" in e or "requests per day" in e or
        " tpd" in e or " rpd" in e or "per-day" in e or "perday" in e or
        ("daily" in e and ("limit" in e or "quota" in e))
    )

RUBRIC_PROMPT_TEMPLATE = """أنت مُقيّم بصري صارم لمقاطع/صور تُستخدم في فيديو يوتيوب شورت.
قيّم مدى ملاءمة هذا الوسيط (صورة أو إطار من فيديو) للنص السردي التالي، وفق 5 معايير محددة، وأرجع تقييمك بصيغة JSON فقط بدون أي نص إضافي:

النص السردي: "{narration}"

المعايير:
1. semantic_match (من 0 إلى 3): التطابق الدلالي مع السياق. 3 = يعكس المعنى الدقيق للنص، 1-2 = مرتبط لكنه عام/سطحي، 0 = لا علاقة له بالسياق.
2. framing (من 0 إلى 2): ملاءمة التأطير لعرض عمودي 9:16. 2 = عمودي أساساً أو العنصر متمركز، 1 = العنصر يتحرك كثيراً وقد يخرج من الإطار، 0 = أفقي والعنصر الرئيسي بالأطراف.
3. quality (من 0 إلى 2): الجودة البصرية. 2 = إضاءة ممتازة ودقة عالية، 1 = دقة مقبولة لكن ألوان باهتة/إضاءة ضعيفة، 0 = دقة منخفضة أو ألوان مزعجة.
4. motion (من 0 إلى 2): ديناميكية الحركة (فقط لو كان المقطع فيديو، وإلا اعتبره 1 افتراضياً للصور الثابتة). 2 = حركة سينمائية واضحة، 1 = حركة بطيئة جداً أو شبه معدومة، 0 = اهتزاز شديد أو حركة فوضوية.
5. cleanliness (من 0 إلى 1): خلو المشهد من نصوص/شعارات/أشخاص ينظرون للكاميرا ويتحدثون. 1 = نظيف تماماً، 0 = يحتوي نصوصاً أو علامات مائية.

أرجع فقط JSON بهذا الشكل بالضبط (بدون أي شرح خارج JSON):
{{"semantic_match": <رقم>, "framing": <رقم>, "quality": <رقم>, "motion": <رقم>, "cleanliness": <رقم>}}
"""


def _parse_rubric_json(text: str) -> dict:
    """يستخرج JSON من رد النموذج حتى لو كان محاطاً بنص أو أسوار ```."""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"لم يُرجع النموذج JSON صالحاً: {text[:200]}")
    data = json.loads(match.group(0))
    return {
        "semantic_match": float(data.get("semantic_match", 0)),
        "framing": float(data.get("framing", 0)),
        "quality": float(data.get("quality", 0)),
        "motion": float(data.get("motion", 0)),
        "cleanliness": float(data.get("cleanliness", 0)),
    }


def _score_from_breakdown(breakdown: dict) -> float:
    return round(sum(breakdown.values()), 2)

# تعريف الموديلات المحدثة لعام 2026
MODEL_TEXT_ADVANCED = "gemini-3.5-flash"  # الموديل الأساسي للمهام البرمجية
MODEL_TEXT_LIGHT = "gemini-3.1-flash-lite" # للمهام السريعة والخفيفة
MODEL_IMAGE_GEN = "gemini-3.1-flash-image"  # الاسم الجديد لـ Nano Banana 2
MODEL_EMBEDDING_NEW = "gemini-embedding-2" # الموديل الجديد الموحد للـ Embeddings

def _get_client(key_type: str) -> genai.Client:
    """key_type: 'light' | 'advanced' | 'image' | 'filter'
    
    منطق المفتاح الجوكر: GEMINI_KEY_IMAGE يُستخدم كمفتاح احتياطي
    لو المفتاح المطلوب غير موجود (لأن توليد الصور أُلغي وهذا المفتاح
    فارغ من مهمته الأصلية).
    """
    if key_type in _clients:
        return _clients[key_type]

    key_map = {
        "light": config.GEMINI_KEY_LIGHT,
        "advanced": config.GEMINI_KEY_ADVANCED,
        "image": config.GEMINI_KEY_IMAGE,
        "filter": config.GEMINI_KEY_FILTER or config.GEMINI_KEY_ADVANCED,
    }
    api_key = key_map.get(key_type)
    
    # المفتاح الجوكر: لو المفتاح المطلوب غير موجود، نستخدم GEMINI_KEY_IMAGE
    if not api_key and key_type != "image" and config.GEMINI_KEY_IMAGE:
        print(f"[GEMINI JOKER] المفتاح '{key_type}' غير موجود. استخدام المفتاح الجوكر (GEMINI_KEY_IMAGE) بدلاً عنه.")
        api_key = config.GEMINI_KEY_IMAGE
    
    if not api_key:
        raise EnvironmentError(f"مفتاح Gemini المطلوب لـ '{key_type}' غير موجود بالأسرار، وحتى المفتاح الجوكر (GEMINI_KEY_IMAGE) غير متاح")

    client = genai.Client(api_key=api_key)
    _clients[key_type] = client
    return client


@with_backoff(max_retries=4, base_delay=3.0)
def _generate_text_internal(prompt: str, model: str, key_type: str, json_mode: bool, temperature: float) -> str:
    """دالة داخلية مع backoff تتولى الطلب الفعلي"""
    client = _get_client(key_type)
    config_kwargs = {"temperature": temperature}
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config_kwargs,
    )
    return response.text


def generate_text(prompt: str, model: str = None, key_type: str = "advanced", json_mode: bool = False,
                  temperature: float = 0.9) -> str:
    """دالة عامة ذكية: إذا استنفدنا حصة الموديل المتقدم، تسقط تلقائياً للموديل الخفيف"""
    target_model = model or (MODEL_TEXT_ADVANCED if key_type == "advanced" else MODEL_TEXT_LIGHT)
    
    try:
        return _generate_text_internal(prompt, target_model, key_type, json_mode, temperature)
    except Exception as e:
        error_str = str(e).lower()
        is_quota = "429" in error_str or "503" in error_str or "quota" in error_str
        
        # إذا نفدت الحصة (429) أو السيرفر مزدحم (503) وكنا نستخدم المفتاح المتقدم، نلجأ للخفيف
        if is_quota and key_type == "advanced" and config.GEMINI_KEY_LIGHT:
            reason = "نفاد الحصة (429)" if "429" in error_str else "ازدحام السيرفر (503)"
            print(f"[GEMINI FALLBACK] {reason} على الموديل المتقدم. الانتقال للموديل الخفيف ({MODEL_TEXT_LIGHT})...")
            try:
                return _generate_text_internal(prompt, MODEL_TEXT_LIGHT, "light", json_mode, temperature)
            except Exception:
                pass
        
        # محاولة المفتاح الجوكر لو الخطأ ليس حصة
        if not is_quota and key_type != "image" and config.GEMINI_KEY_IMAGE:
            alert_key_error("Gemini", key_type, str(e))
            print(f"[GEMINI JOKER] خطأ غير حصة بالمفتاح '{key_type}'. تجربة المفتاح الجوكر...")
            try:
                # نحتاج عميل جديد بالمفتاح الجوكر
                joker_client = genai.Client(api_key=config.GEMINI_KEY_IMAGE)
                config_kwargs = {"temperature": temperature}
                if json_mode:
                    config_kwargs["response_mime_type"] = "application/json"
                response = joker_client.models.generate_content(
                    model=target_model, contents=prompt, config=config_kwargs,
                )
                return response.text
            except Exception as joker_e:
                print(f"[GEMINI JOKER] فشل المفتاح الجوكر أيضاً: {joker_e}")
        elif not is_quota:
            # خطأ غير حصة وليس لدينا جوكر — نرسل تنبيه
            alert_key_error("Gemini", key_type, str(e))
        
        raise e


@with_backoff(max_retries=4, base_delay=3.0)
def generate_image(prompt: str, model: str = None) -> bytes:
    """يستخدم مفتاح الصور وموديل Nano Banana 2 الجديد."""
    client = _get_client("image")
    target_model = model or MODEL_IMAGE_GEN
    response = client.models.generate_content(
        model=target_model,
        contents=prompt,
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return part.inline_data.data
    raise RuntimeError("لم يرجع Gemini أي صورة بالاستجابة")


def _verify_media_internal(image_path: str, narration: str, model: str, key_type: str) -> bool:
    client = _get_client(key_type)
    prompt = (
        "You are a strict visual quality inspector for a YouTube video. "
        "Does this image clearly and literally show exactly what is described in the narration? "
        "If the narration mentions 'video games', 'digital graphics', or 'pixels', and the image shows a physical board game (like chess or foosball), you MUST answer NO. "
        "If the image is completely unrelated to the core subject of the narration, answer NO. "
        f"Answer ONLY with YES or NO.\n\nNarration: {narration}"
    )
    
    with Image.open(image_path) as img:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, img],
            config={"temperature": 0.0} # Strict deterministic
        )
        text = response.text.strip().upper()
        return "YES" in text

class GeminiVerificationUnavailable(Exception):
    """تُرفع عندما تفشل كل نماذج Gemini بالتحقق البصري (429/503/انقطاع
    اتصال/أي خطأ آخر) — تسمح لـ media_relevance_checker.py بتفعيل حارس
    الجودة المحلي (CLIP) كطبقة حماية ثانية بدل الموافقة التلقائية العمياء."""
    pass


class GeminiDailyQuotaExceeded(GeminiVerificationUnavailable):
    """تُرفع تحديداً لو اكتُشف أن فشل كل نماذج Gemini الأربعة كان بسبب
    تجاوز حصة يومية (TPD/RPD) وليس حصة دقيقة/ثانية عابرة — تسمح لـ
    analysis_engine.py بالتبديل الفوري والدائم لهذه الطبقة إلى Puter لبقية
    هذا التشغيل، بدل إعادة محاولات مضمونة الفشل."""
    pass


def verify_media_relevance(image_path: str, narration: str) -> bool:
    """
    تتحقق ما إذا كانت الصورة أو إطار الفيديو يتطابق مع نص السرد (التحقق البصري)
    تستخدم سلسلة من 4 نماذج لتفادي نفاد الحصة (429) على الطبقة المجانية.

    ملاحظة: لو فشلت كل النماذج الأربعة لأي سبب، هذه الدالة ترفع
    GeminiVerificationUnavailable (أو GeminiDailyQuotaExceeded تحديداً لو
    كانت حصة يومية) بدل إرجاع True تلقائياً — القرار النهائي (الموافقة أو
    تفعيل CLIP) أصبح مسؤولية media_relevance_checker.py.
    """
    if is_daily_exhausted():
        raise GeminiDailyQuotaExceeded("تم استنفاد الحصة اليومية لكل نماذج Gemini سابقاً بهذا التشغيل")

    models_to_try = [
        (MODEL_TEXT_ADVANCED, "filter"),        # gemini-3.5-flash (أفضل جودة)
        (MODEL_TEXT_LIGHT, "filter"),           # gemini-3.1-flash-lite (سريع وخفيف)
        ("gemini-2.5-flash", "filter"),         # الجيل السابق (قوي)
        ("gemini-2.5-flash-lite", "filter"),    # الجيل السابق (خفيف)
    ]

    last_error = None
    for i, (model_name, key_type) in enumerate(models_to_try):
        try:
            return _verify_media_internal(image_path, narration, model_name, key_type)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_last = i == len(models_to_try) - 1
            if "429" in error_str or "503" in error_str or "quota" in error_str:
                if not is_last:
                    next_model = models_to_try[i + 1][0]
                    print(f"[GEMINI CASCADE] الموديل {model_name} غير متاح ({'429' if '429' in error_str else '503'}). الانتقال فوراً إلى {next_model}...")
                    continue  # Try the next model
            else:
                # خطأ غير متعلق بالحصة (شبكة معطوبة، استجابة غير متوقعة...)؛
                # نجرب النموذج التالي أيضاً بدل الاستسلام فوراً
                if not is_last:
                    next_model = models_to_try[i + 1][0]
                    print(f"[GEMINI ERROR] خطأ بالموديل {model_name}: {e}. تجربة {next_model}...")
                    continue

    print(f"[GEMINI ERROR] فشل التحقق البصري باستخدام كل النماذج الأربعة المتاحة: {last_error}")
    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        _mark_daily_exhausted()
        raise GeminiDailyQuotaExceeded(str(last_error))
    # إرسال تنبيه فقط لو الخطأ ليس حصة
    if not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
        alert_key_error("Gemini Vision", "filter", str(last_error))
    raise GeminiVerificationUnavailable(str(last_error))


def _score_media_internal(image_path: str, narration: str, model: str, key_type: str) -> dict:
    client = _get_client(key_type)
    prompt = RUBRIC_PROMPT_TEMPLATE.format(narration=narration)
    with Image.open(image_path) as img:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, img],
            config={"temperature": 0.0, "response_mime_type": "application/json"},
        )
        breakdown = _parse_rubric_json(response.text)
        score = _score_from_breakdown(breakdown)
        return {"score": score, "passed": score > 7, "breakdown": breakdown, "layer": "gemini", "model": model}


def score_media_relevance(image_path: str, narration: str) -> dict:
    """نظام تقييم من 10 (راجع RUBRIC_PROMPT_TEMPLATE). يُقبل الوسيط لو
    score > 7. نفس تدرّج الـ 4 نماذج، مع فاصل 15 ثانية إلزامي بين الطلبات
    (_VERIFY_RATE_LIMITER) لأن هذه الطبقة تعمل الآن بالتوازي مع Groq.

    ترفع GeminiDailyQuotaExceeded فوراً بلا أي محاولة لو سبق أن اكتُشف
    استنفاد الحصة اليومية بهذا التشغيل (راجع is_daily_exhausted)."""
    if is_daily_exhausted():
        raise GeminiDailyQuotaExceeded("تم استنفاد الحصة اليومية لكل نماذج Gemini سابقاً بهذا التشغيل")

    models_to_try = [
        (MODEL_TEXT_ADVANCED, "filter"),
        (MODEL_TEXT_LIGHT, "filter"),
        ("gemini-2.5-flash", "filter"),
        ("gemini-2.5-flash-lite", "filter"),
    ]

    last_error = None
    for i, (model_name, key_type) in enumerate(models_to_try):
        _VERIFY_RATE_LIMITER.wait()
        try:
            return _score_media_internal(image_path, narration, model_name, key_type)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_last = i == len(models_to_try) - 1
            is_quota = "429" in error_str or "503" in error_str or "quota" in error_str
            next_model = models_to_try[i + 1][0] if not is_last else None
            if is_quota and not is_last:
                print(f"[GEMINI CASCADE] الموديل {model_name} غير متاح (حصة/ازدحام). الانتقال فوراً إلى {next_model}...")
                continue
            elif not is_quota and not is_last:
                print(f"[GEMINI ERROR] خطأ بالموديل {model_name}: {e}. تجربة {next_model}...")
                continue

    print(f"[GEMINI ERROR] فشل تقييم الوسيط باستخدام كل النماذج المتاحة: {last_error}")
    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        _mark_daily_exhausted()
        raise GeminiDailyQuotaExceeded(str(last_error))
    if not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
        alert_key_error("Gemini Vision", "filter", str(last_error))
    raise GeminiVerificationUnavailable(str(last_error))


@with_backoff(max_retries=3, base_delay=2.0)
def get_embedding(text: str, key_type: str = "light") -> list[float]:
    """يستخدم الموديل الجديد الموحد Gemini Embedding 2."""
    client = _get_client(key_type)
    result = client.models.embed_content(model=MODEL_EMBEDDING_NEW, contents=text)
    return result.embeddings[0].values
