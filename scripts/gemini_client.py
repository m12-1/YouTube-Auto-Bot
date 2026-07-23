"""
gemini_client.py
مُحدث لعام 2026: يدعم موديلات Gemini 3 و Nano Banana الجديدة.
غلاف موحد فوق google-genai SDK.
"""
import json
import re
import threading

from google import genai
from scripts import config, content_policy
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
# ساعات). لو اكتشفنا أن كل نماذج Gemini الخمسة فشلت برسالة تشير صراحة
# لحصة يومية، نضع علامة داخل الذاكرة ونتخطى Gemini فوراً بلا محاولة لبقية
# هذا التشغيل (بدل إعادة محاولات مضمونة الفشل على 5 نماذج في كل مشهد جديد).
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

# قائمة مسطحة من كل الكلمات المحظورة العامة (content_policy) — تُضاف كقيد
# سلبي صارم (negative prompt) في كل طلب تقييم بصري.
_GLOBAL_NEGATIVE_KEYWORDS = ", ".join(sorted(set(content_policy.all_blocked_keywords_flat())))


def _build_context_block(topic_context: str) -> str:
    """يبني كتلة سياق ثابتة (المزاج/الهوية البصرية للفيديو + قائمة محظورات)
    تُضاف لكل طلب تقييم بصري، بدل إرسال الكلمة المفتاحية للمشهد وحدها بمعزل
    عن سياق الفيديو الكامل. هذا يمنع قبول مشاهد 'مطابقة سطحياً' لكنها تخرج
    عن الهوية البصرية العامة للفيديو (مثال: مقطع شلال طبيعي بفيديو عن ألعاب
    كمبيوتر لمجرد ورود كلمة عامة بالنص)."""
    mood = (topic_context or "").strip()
    mood_line = (
        f'السياق العام/الهوية البصرية لكامل هذا الفيديو: "{mood}". احكم على مدى '
        f'مطابقة هذا المشهد للنص السردي أدناه ضمن هذا السياق تحديداً وليس بمعزل عنه — '
        f'ارفض أي مشهد يخرج عن الهوية البصرية العامة حتى لو بدا مرتبطاً سطحياً بكلمة واحدة بالنص.\n'
        if mood else ""
    )
    return (
        f"{mood_line}"
        f"قيود صارمة (Negative Prompts) — ارفض المشهد فوراً (semantic_match = 0) لو ظهر به أي من: "
        f"{_GLOBAL_NEGATIVE_KEYWORDS}، أو أي عنصر مرئي لا علاقة له إطلاقاً بموضوع الفيديو/السياق أعلاه.\n"
    )


RUBRIC_PROMPT_TEMPLATE = """أنت مُقيّم بصري صارم لمقاطع/صور تُستخدم في فيديو يوتيوب شورت.
قيّم مدى ملاءمة هذا الوسيط (صورة أو إطار من فيديو) للنص السردي التالي، وفق 5 معايير محددة، وأرجع تقييمك بصيغة JSON فقط بدون أي نص إضافي:

{context_block}
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


# أقصى عدد مشاهد تُرسَل بطلب واحد لـ Gemini بدل طلب منفصل لكل مشهد (حسب
# الطلب: "نرسل عدد من المشاهد حسب عدد المشاهد التي يدعمها في كل طلب"). رقم
# متحفظ يوازن بين توفير الحصة اليومية (طلب واحد بدل N طلب) وحجم الاستجابة/
# الدقة (كل صورة تستهلك توكنز كثيرة، وكثرتها بنفس الطلب قد تُربك النموذج).
MAX_BATCH_SCENES_PER_REQUEST = 6

# نفس رأس رسالة التقييم لكن للدفعة: يُذكر فيها مرة واحدة فقط سياق/هوية
# الفيديو العامة والمحظورات (بدل تكرارها لكل مشهد بطلب منفصل)، ثم تُدرَج كل
# المشاهد بالترتيب — كل مشهد مسبوق بمعرّفه الفريد (اسم ملفه) ونصه السردي
# الخاص، ومتبوعاً مباشرة بصورته.
BATCH_RUBRIC_HEADER_TEMPLATE = """أنت مُقيّم بصري صارم لمقاطع/صور تُستخدم في فيديو يوتيوب شورت واحد.
سأرسل لك أدناه {count} مشهداً مختلفاً من نفس الفيديو دفعة واحدة. لكل مشهد معرّف فريد (اسم ملفه) ونص سردي خاص به.
قيّم كل مشهد على حدة وفق نص ذلك المشهد تحديداً فقط (لا تخلط بين المشاهد ولا تقارنها ببعضها)، وفق 5 معايير محددة:

{context_block}
المعايير لكل مشهد (0 إلى 10 بالمجموع):
1. semantic_match (من 0 إلى 3): التطابق الدلالي مع نص ذلك المشهد. 3 = يعكس المعنى الدقيق، 1-2 = مرتبط لكنه عام/سطحي، 0 = لا علاقة له بالسياق.
2. framing (من 0 إلى 2): ملاءمة التأطير لعرض عمودي 9:16. 2 = عمودي أساساً أو العنصر متمركز، 1 = العنصر يتحرك كثيراً وقد يخرج من الإطار، 0 = أفقي والعنصر الرئيسي بالأطراف.
3. quality (من 0 إلى 2): الجودة البصرية. 2 = إضاءة ممتازة ودقة عالية، 1 = دقة مقبولة لكن ألوان باهتة/إضاءة ضعيفة، 0 = دقة منخفضة أو ألوان مزعجة.
4. motion (من 0 إلى 2): ديناميكية الحركة (فقط لو كان المقطع فيديو، وإلا اعتبره 1 افتراضياً للصور الثابتة). 2 = حركة سينمائية واضحة، 1 = حركة بطيئة جداً أو شبه معدومة، 0 = اهتزاز شديد أو حركة فوضوية.
5. cleanliness (من 0 إلى 1): خلو المشهد من نصوص/شعارات/أشخاص ينظرون للكاميرا ويتحدثون. 1 = نظيف تماماً، 0 = يحتوي نصوصاً أو علامات مائية.

المشاهد أدناه بالترتيب، كل مشهد مسبوق بعنوان يحمل معرّفه الفريد ونصه السردي، متبوعاً مباشرة بصورته:
"""

# يُضاف بعد آخر مشهد وآخر صورة — يذكّر Gemini بكل المعرّفات المطلوبة بالضبط
# حتى لا ينسى مشهداً أو يخترع معرّفاً غير موجود.
BATCH_RUBRIC_FOOTER_TEMPLATE = """
انتهت كل المشاهد ({count} مشهداً). أرجع الآن كائن JSON واحد فقط (بدون أي نص أو شرح خارج JSON، وبدون أسوار ```)، مفاتيحه هي معرّفات المشاهد بالضبط كما وردت أعلاه، وقيمة كل مفتاح هي تقييم ذلك المشهد بنفس الصيغة التالية:
{{"<معرّف المشهد>": {{"semantic_match": <رقم>, "framing": <رقم>, "quality": <رقم>, "motion": <رقم>, "cleanliness": <رقم>}}, ...}}

يجب أن يحتوي الـ JSON على تقييم لكل معرّف من هذه المعرّفات بالضبط، بدون نقصان أو زيادة أو تغيير بالتهجئة:
{ids}
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


def _parse_batch_json(text: str) -> dict:
    """يستخرج كائن JSON كامل (معرّف مشهد -> breakdown) من رد الدفعة، حتى لو
    كان محاطاً بنص أو أسوار ```. نفس فكرة _parse_rubric_json لكن يرجع
    القاموس بأكمله بدل مستوى واحد من الحقول (لأن الدفعة تحتوي عدة مشاهد)."""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"لم يُرجع النموذج JSON صالحاً للدفعة: {text[:200]}")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"رد الدفعة ليس كائن JSON بمفاتيح معرّفات المشاهد: {text[:200]}")
    return data

# تعريف الموديلات المحدثة لعام 2026
MODEL_TEXT_ADVANCED = "gemini-3.5-flash"  # الموديل الأساسي للمهام البرمجية
MODEL_TEXT_LIGHT = "gemini-3.1-flash-lite" # للمهام السريعة والخفيفة
MODEL_TEXT_LIGHT_35 = "gemini-3.5-flash-lite"  # الأحدث والأسرع — أول نموذج يُجرَّب الآن بطبقة التحليل البصري
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
        "filter2": config.GEMINI_KEY_FILTER_2,  # مفتاح إضافي اختياري لمضاعفة حصة التحقق البصري
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


@with_backoff(max_retries=4, base_delay=3.0, fail_fast_predicate=_is_daily_quota_message)
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


def _verify_media_internal(image_path: str, narration: str, model: str, key_type: str,
                            topic_context: str = "") -> bool:
    client = _get_client(key_type)
    mood = (topic_context or "").strip()
    mood_clause = (
        f"The overall visual identity/mood of this whole video is: \"{mood}\". Judge this scene "
        f"WITHIN that context — reject it if it looks out of place for that mood/identity even if it "
        f"superficially matches a single word in the narration. "
    ) if mood else ""
    negative_clause = (
        f"You MUST answer NO if the image shows any of: {_GLOBAL_NEGATIVE_KEYWORDS}, "
        f"or anything visually unrelated to the video's topic/mood above. "
    )
    prompt = (
        "You are a strict visual quality inspector for a YouTube video. "
        "Does this image clearly and literally show exactly what is described in the narration? "
        + mood_clause + negative_clause +
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

# نماذج التحقق البصري المتاحة رسمياً حالياً (الأحدث فالأقدم) — تُجرَّب
# بالتسلسل على مفتاح "filter"، ثم تُعاد كاملة على مفتاح "filter2" لو كان
# مُعرَّفاً (GEMINI_KEY_FILTER_2) قبل الاستسلام والانتقال لـ Puter.
_FILTER_MODELS = [
    MODEL_TEXT_LIGHT_35,     # gemini-3.5-flash-lite (أحدث وأسرع)
    MODEL_TEXT_ADVANCED,     # gemini-3.5-flash (أفضل جودة)
    MODEL_TEXT_LIGHT,        # gemini-3.1-flash-lite
    "gemini-2.5-flash",      # الجيل السابق (قوي)
    "gemini-2.5-flash-lite", # الجيل السابق (خفيف)
]


def _build_filter_cascade(only_key_type: str = None) -> list:
    """يبني قائمة (model, key_type):
    - only_key_type=None (الافتراضي، الاستخدام العام): 5 نماذج على المفتاح
      الأساسي 'filter'، ثم -لو كان GEMINI_KEY_FILTER_2 معرّفاً- نفس الـ5
      نماذج مجدداً على المفتاح الثاني 'filter2' (حصة يومية منفصلة تماماً)
      كتصعيد تسلسلي عند فشل الأول.
    - only_key_type='filter' أو 'filter2': يبني تدرّج الـ5 نماذج على هذا
      المفتاح بعينه فقط، دون التبديل للمفتاح الآخر — يُستخدم من
      analysis_engine.py لتشغيل مفتاحي Gemini كعاملين (lanes) مستقلين
      بالتوازي الحقيقي بنفس الوقت (كل مفتاح يفحص مجموعة مشاهد مختلفة)
      بدل أن يكون المفتاح الثاني مجرد احتياط تسلسلي للأول.
    """
    if only_key_type:
        return [(m, only_key_type) for m in _FILTER_MODELS]
    cascade = [(m, "filter") for m in _FILTER_MODELS]
    if config.GEMINI_KEY_FILTER_2:
        cascade += [(m, "filter2") for m in _FILTER_MODELS]
    return cascade


class GeminiVerificationUnavailable(Exception):
    """تُرفع عندما تفشل كل نماذج Gemini بالتحقق البصري (429/503/انقطاع
    اتصال/أي خطأ آخر) — تسمح لـ media_relevance_checker.py بتفعيل حارس
    الجودة المحلي (CLIP) كطبقة حماية ثانية بدل الموافقة التلقائية العمياء."""
    pass


class GeminiDailyQuotaExceeded(GeminiVerificationUnavailable):
    """تُرفع تحديداً لو اكتُشف أن فشل كل نماذج Gemini الخمسة كان بسبب
    تجاوز حصة يومية (TPD/RPD) وليس حصة دقيقة/ثانية عابرة — تسمح لـ
    analysis_engine.py بالتبديل الفوري والدائم لهذه الطبقة إلى Puter لبقية
    هذا التشغيل، بدل إعادة محاولات مضمونة الفشل."""
    pass


def verify_media_relevance(image_path: str, narration: str, topic_context: str = "",
                            only_key_type: str = None) -> bool:
    """
    تتحقق ما إذا كانت الصورة أو إطار الفيديو يتطابق مع نص السرد (التحقق البصري)
    تستخدم سلسلة من 5 نماذج لتفادي نفاد الحصة (429) على الطبقة المجانية.

    topic_context: المزاج/الهوية البصرية الثابتة للفيديو كاملاً — تُمرَّر
    دائماً مع المشهد بدل إرسال الكلمة المفتاحية وحدها (راجع _build_context_block).
    only_key_type: لو مُحدَّد ('filter' أو 'filter2')، يقيّد التدرّج على هذا
    المفتاح فقط (يُستخدم لتشغيل عاملَي Gemini بالتوازي الحقيقي).

    ملاحظة: لو فشلت كل النماذج الخمسة لأي سبب، هذه الدالة ترفع
    GeminiVerificationUnavailable (أو GeminiDailyQuotaExceeded تحديداً لو
    كانت حصة يومية) بدل إرجاع True تلقائياً — القرار النهائي (الموافقة أو
    تفعيل CLIP) أصبح مسؤولية media_relevance_checker.py.
    """
    if is_daily_exhausted():
        raise GeminiDailyQuotaExceeded("تم استنفاد الحصة اليومية لكل نماذج Gemini سابقاً بهذا التشغيل")

    models_to_try = _build_filter_cascade(only_key_type)

    last_error = None
    for i, (model_name, key_type) in enumerate(models_to_try):
        try:
            return _verify_media_internal(image_path, narration, model_name, key_type, topic_context)
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

    print(f"[GEMINI ERROR] فشل التحقق البصري باستخدام كل النماذج/المفاتيح المتاحة ({len(models_to_try)} محاولة): {last_error}")
    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        _mark_daily_exhausted()
        raise GeminiDailyQuotaExceeded(str(last_error))
    # إرسال تنبيه فقط لو الخطأ ليس حصة
    if not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
        alert_key_error("Gemini Vision", "filter", str(last_error))
    raise GeminiVerificationUnavailable(str(last_error))


def _score_media_internal(image_path: str, narration: str, model: str, key_type: str,
                           topic_context: str = "") -> dict:
    client = _get_client(key_type)
    prompt = RUBRIC_PROMPT_TEMPLATE.format(
        narration=narration, context_block=_build_context_block(topic_context)
    )
    with Image.open(image_path) as img:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, img],
            config={"temperature": 0.0, "response_mime_type": "application/json"},
        )
        breakdown = _parse_rubric_json(response.text)
        score = _score_from_breakdown(breakdown)
        return {"score": score, "passed": score > 7, "breakdown": breakdown, "layer": "gemini", "model": model}


def _score_media_batch_internal(items: list, model: str, key_type: str,
                                 topic_context: str = "") -> dict:
    """يبني طلباً واحداً يحتوي كل مشاهد الدفعة (نص + صورة لكل مشهد بالتتابع)
    ويرسله بطلب Gemini واحد فقط، بدل طلب منفصل لكل مشهد.

    items: قائمة عناصر {"id": معرّف فريد (اسم الملف عادة), "path": مسار
    الصورة/الإطار, "narration": النص السردي الخاص بهذا المشهد فقط}.

    يرجع قاموس {id: {"score", "passed", "breakdown", "layer", "model"}} —
    نفس شكل نتيجة score_media_relevance لكل معرّف بالدفعة."""
    client = _get_client(key_type)
    ids = [item["id"] for item in items]

    header = BATCH_RUBRIC_HEADER_TEMPLATE.format(
        count=len(items), context_block=_build_context_block(topic_context)
    )
    footer = BATCH_RUBRIC_FOOTER_TEMPLATE.format(count=len(items), ids=", ".join(ids))

    contents = [header]
    opened_images = []
    try:
        for item in items:
            contents.append(f'\n--- المشهد "{item["id"]}" ---\nالنص السردي لهذا المشهد فقط: "{item["narration"]}"\n')
            img = Image.open(item["path"])
            img.load()  # نضمن قراءة البيانات كاملة قبل إغلاق أي ملف مؤقت خارجياً
            opened_images.append(img)
            contents.append(img)
        contents.append(footer)

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config={"temperature": 0.0, "response_mime_type": "application/json"},
        )
    finally:
        for img in opened_images:
            try:
                img.close()
            except Exception:
                pass

    raw = _parse_batch_json(response.text)
    results = {}
    for item in items:
        entry = raw.get(item["id"])
        if not isinstance(entry, dict):
            raise ValueError(f"لم يرجع Gemini تقييماً للمشهد بالمعرّف '{item['id']}' ضمن رد الدفعة")
        breakdown = _parse_rubric_json(json.dumps(entry))
        score = _score_from_breakdown(breakdown)
        results[item["id"]] = {
            "score": score, "passed": score > 7, "breakdown": breakdown,
            "layer": "gemini", "model": model,
        }
    return results


def score_media_relevance_batch(items: list, topic_context: str = "",
                                 only_key_type: str = None) -> dict:
    """نسخة الدفعة من score_media_relevance: بدل إرسال طلب منفصل لكل مشهد،
    نرسل حتى MAX_BATCH_SCENES_PER_REQUEST مشهداً بطلب واحد — نُخبر Gemini
    بسياق الفيديو العام (المزاج/الهوية البصرية + المحظورات) مرة واحدة فقط،
    ثم نُدرج كل مشهد بمعرّفه (عادة اسم الملف) ونصه السردي وصورته، ونطلب
    تقييماً منفصلاً لكل معرّف بنفس رد الـ JSON الواحد.

    items: قائمة {"id", "path", "narration"} — الحد الأقصى المُوصى به لكل
    استدعاء هو MAX_BATCH_SCENES_PER_REQUEST (المستدعي مسؤول عن التقسيم لو
    كانت الدفعة أكبر).

    نفس تدرّج الـ 5 نماذج × المفاتيح المستخدم بـ score_media_relevance — لو
    فشلت الدفعة كاملة بنموذج مُعيّن (خطأ حصة/شبكة/JSON) نجرب النموذج التالي
    بنفس الدفعة كاملة، وليس مشهداً مشهداً.

    ترفع GeminiDailyQuotaExceeded/GeminiVerificationUnavailable تماماً كما
    تفعل النسخة المفردة لو فشلت كل النماذج — القرار عندها (تقسيم الدفعة
    ومعالجتها فردياً بعامل آخر، أو تسليمها لـ Puter/CLIP) مسؤولية
    analysis_engine.py تماماً كما هو الحال مع المشهد المفرد."""
    if not items:
        return {}
    if is_daily_exhausted():
        raise GeminiDailyQuotaExceeded("تم استنفاد الحصة اليومية لكل نماذج Gemini سابقاً بهذا التشغيل")

    models_to_try = _build_filter_cascade(only_key_type)

    last_error = None
    for i, (model_name, key_type) in enumerate(models_to_try):
        _VERIFY_RATE_LIMITER.wait()
        try:
            return _score_media_batch_internal(items, model_name, key_type, topic_context)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_last = i == len(models_to_try) - 1
            is_quota = "429" in error_str or "503" in error_str or "quota" in error_str
            next_model = models_to_try[i + 1][0] if not is_last else None
            if is_quota and not is_last:
                print(f"[GEMINI BATCH CASCADE] الموديل {model_name} غير متاح (حصة/ازدحام) لدفعة من {len(items)} مشهداً. الانتقال فوراً إلى {next_model}...")
                continue
            elif not is_quota and not is_last:
                print(f"[GEMINI BATCH ERROR] خطأ بالموديل {model_name} على دفعة من {len(items)} مشهداً: {e}. تجربة {next_model}...")
                continue

    print(f"[GEMINI BATCH ERROR] فشل تقييم الدفعة ({len(items)} مشهداً) باستخدام كل النماذج المتاحة: {last_error}")
    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        _mark_daily_exhausted()
        raise GeminiDailyQuotaExceeded(str(last_error))
    if not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
        alert_key_error("Gemini Vision (batch)", "filter", str(last_error))
    raise GeminiVerificationUnavailable(str(last_error))


def score_media_relevance(image_path: str, narration: str, topic_context: str = "",
                           only_key_type: str = None) -> dict:
    """نظام تقييم من 10 (راجع RUBRIC_PROMPT_TEMPLATE). يُقبل الوسيط لو
    score > 7. نفس تدرّج الـ 5 نماذج، مع فاصل 15 ثانية إلزامي بين الطلبات
    (_VERIFY_RATE_LIMITER) لأن هذه الطبقة تعمل الآن بالتوازي مع Groq.

    topic_context: يُمرَّر دائماً كمتغير ثابت (المزاج/الهوية البصرية لكامل
    الفيديو) مع النص السردي، ويُستخدم أيضاً لبناء قائمة المحظورات (راجع
    _build_context_block) بدل الاعتماد فقط على الكلمة المفتاحية للمشهد.
    only_key_type: لو مُحدَّد، يقيّد التدرّج على مفتاح Gemini واحد بعينه
    ('filter' أو 'filter2') — يُستخدم لتشغيل عاملين (lanes) بمفتاحين
    مختلفين بالتوازي الحقيقي بنفس الوقت بدل التصعيد التسلسلي بينهما.

    ترفع GeminiDailyQuotaExceeded فوراً بلا أي محاولة لو سبق أن اكتُشف
    استنفاد الحصة اليومية بهذا التشغيل (راجع is_daily_exhausted)."""
    if is_daily_exhausted():
        raise GeminiDailyQuotaExceeded("تم استنفاد الحصة اليومية لكل نماذج Gemini سابقاً بهذا التشغيل")

    models_to_try = _build_filter_cascade(only_key_type)

    last_error = None
    for i, (model_name, key_type) in enumerate(models_to_try):
        _VERIFY_RATE_LIMITER.wait()
        try:
            return _score_media_internal(image_path, narration, model_name, key_type, topic_context)
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
