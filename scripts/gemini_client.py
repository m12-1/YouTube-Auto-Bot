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
from scripts.telegram_alerts import alert_key_error, send_alert

_clients = {}

# يضمن 15 ثانية على الأقل بين طلب وآخر لطبقة التحقق البصري لكل مفتاح Gemini
# على حدة (تشغّل الآن بالتوازي مع Groq، فبدون هذا القفل يمكن أن تُستهلك
# الحصة المجانية أسرع بكثير من المسموح).
#
# مهم: محدِّد منفصل *لكل key_type* (filter / filter2 / light / advanced...)
# وليس محدِّداً عالمياً واحداً مشتركاً. السبب: كل مفتاح Gemini هنا قد ينتمي
# لحساب Google / مشروع مختلف تماماً بحصة (RPM) مستقلة بالكامل عن باقي
# المفاتيح. لو استُخدم قفل واحد مشترك بين "filter" و"filter2" مثلاً (كما
# كان سابقاً)، فإن طلبات المفتاح الثاني تنتظر بلا داعٍ فاصل 15 ثانية
# متأثراً بطلبات المفتاح الأول رغم أن حصتيهما منفصلتان تماماً — ما يُبطل
# نصف الفائدة من تشغيل عاملين (lanes) بمفتاحين مختلفين بالتوازي الحقيقي.
# كل مفتاح الآن يُحترم حده الخاص باستقلال تام عن غيره.
_verify_rate_limiters_lock = threading.Lock()
_verify_rate_limiters: dict = {}


def _get_verify_rate_limiter(key_type: str) -> RateLimiter:
    """يرجع (وينشئ عند الحاجة) محدِّد معدّل مستقل خاص بهذا الـ key_type
    فقط، بدل قفل عالمي واحد مشترك بين كل المفاتيح."""
    with _verify_rate_limiters_lock:
        limiter = _verify_rate_limiters.get(key_type)
        if limiter is None:
            limiter = RateLimiter(min_interval=15.0)
            _verify_rate_limiters[key_type] = limiter
        return limiter

# --- كاش استنفاد المفاتيح بالذاكرة ---
# تتبع المفاتيح التي استنفدت الحصة اليومية بالكامل لإيقافها.
_exhausted_keys_lock = threading.Lock()
_exhausted_keys = set()

def is_key_exhausted(api_key: str) -> bool:
    if not api_key:
        return False
    with _exhausted_keys_lock:
        return api_key in _exhausted_keys

def _mark_key_exhausted(api_key: str) -> None:
    if not api_key:
        return
    with _exhausted_keys_lock:
        if api_key not in _exhausted_keys:
            _exhausted_keys.add(api_key)
            print(f"[GEMINI] اكتُشف استنفاد الحصة اليومية للمفتاح {api_key[:8]}... سيتم إزالته من دورة التبديل طوال هذا التشغيل.")

# --- آلية Thread-Safe لمنع تضارب المفاتيح والنماذج ---
# تمنع أكثر من Thread من استخدام نفس (API Key + Model) بنفس اللحظة.
_key_model_locks = {}
_key_model_locks_mutex = threading.Lock()

def _try_acquire_key_model_lock(api_key: str, model: str) -> bool:
    key = (api_key, model)
    with _key_model_locks_mutex:
        if key not in _key_model_locks:
            _key_model_locks[key] = threading.Lock()
        lock = _key_model_locks[key]
    return lock.acquire(blocking=False)

def _release_key_model_lock(api_key: str, model: str) -> None:
    key = (api_key, model)
    with _key_model_locks_mutex:
        lock = _key_model_locks.get(key)
    if lock:
        try:
            lock.release()
        except RuntimeError:
            pass


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
النص السردي/البحث: "{narration}"

المعايير (الإجمالي 10 درجات، والمطابقة الدلالية هي الأهم):
1. semantic_match (من 0 إلى 6): التطابق الدلالي مع السياق. 6 = يعكس المعنى الدقيق تماماً بشكل مباشر وواضح، 3-5 = مرتبط لكنه عام أو غير مباشر، 0-2 = لا علاقة له بالسياق الحقيقي أو مجازي بشكل خاطئ. (إذا كان 2 أو أقل، سيتم رفض المشهد تلقائياً).
2. quality (من 0 إلى 1.5): الجودة البصرية. 1.5 = إضاءة ممتازة ودقة عالية، 0.5 = دقة مقبولة، 0 = دقة منخفضة.
3. framing (من 0 إلى 1): ملاءمة التأطير. 1 = ممتازة لعرض عمودي 9:16، 0 = سيئة.
4. motion (من 0 إلى 1): ديناميكية الحركة. 1 = حركة واضحة، 0 = شبه معدومة أو فوضوية.
5. cleanliness (من 0 إلى 0.5): خلو المشهد من نصوص/شعارات. 0.5 = نظيف، 0 = نصوص/علامات مائية.

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
سأرسل لك أدناه {count} مشهداً مختلفاً من نفس الفيديو دفعة واحدة. لكل مشهد معرّف فريد (اسم ملفه) ونصه الخاص به.
قيّم كل مشهد على حدة وفق نص ذلك المشهد تحديداً فقط (لا تخلط بين المشاهد ولا تقارنها ببعضها)، وفق 5 معايير محددة:

{context_block}
المعايير (الإجمالي 10 درجات، والمطابقة الدلالية هي الأهم):
1. semantic_match (من 0 إلى 6): التطابق الدلالي مع نص ذلك المشهد. 6 = يعكس المعنى الدقيق تماماً بشكل مباشر وواضح، 3-5 = مرتبط لكنه عام أو غير مباشر، 0-2 = لا علاقة له بالسياق الحقيقي أو مجازي بشكل خاطئ. (إذا كان 2 أو أقل، سيتم رفض المشهد تلقائياً).
2. quality (من 0 إلى 1.5): الجودة البصرية. 1.5 = إضاءة ممتازة ودقة عالية، 0.5 = دقة مقبولة، 0 = دقة منخفضة.
3. framing (من 0 إلى 1): ملاءمة التأطير. 1 = ممتازة لعرض عمودي 9:16، 0 = سيئة.
4. motion (من 0 إلى 1): ديناميكية الحركة. 1 = حركة واضحة، 0 = شبه معدومة أو فوضوية.
5. cleanliness (من 0 إلى 0.5): خلو المشهد من نصوص/شعارات. 0.5 = نظيف، 0 = نصوص/علامات مائية.

المشاهد أدناه بالترتيب، كل مشهد مسبوق بعنوان يحمل معرّفه الفريد ونصه، متبوعاً مباشرة بصورته:
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


@with_backoff(max_retries=4, base_delay=3.0, fail_fast_predicate=_is_daily_quota_message, alert_level="warning")
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
            except Exception as fallback_e:
                # هنا فعلاً كل مسارات التعافي انتهت (لا يوجد جوكر لأخطاء
                # حصة أصلاً — الجوكر مخصص لأخطاء المفاتيح المعطّلة فقط) —
                # هذا هو المكان الصحيح لتنبيه "error" حقيقي، وليس داخل
                # with_backoff قبل تجربة الموديل الخفيف.
                send_alert(
                    f"فشلت generate_text نهائياً بعد تجربة الموديل المتقدم والخفيف معاً: {fallback_e}",
                    level="error",
                )
                raise fallback_e
        
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
                send_alert(
                    f"فشلت generate_text نهائياً بعد تجربة المفتاح '{key_type}' والمفتاح الجوكر معاً: {joker_e}",
                    level="error",
                )
        elif not is_quota:
            # خطأ غير حصة وليس لدينا جوكر — alert_key_error أعلاه سبق أن
            # أرسل تنبيهاً بمستوى "error"؛ هذا هو فعلاً آخر مسار متاح.
            pass
        
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
    models_to_try = _build_filter_cascade(only_key_type)
    
    # فلترة الموديلات لتخطي المفاتيح المستنفدة تماماً
    valid_models = []
    for model, key_type in models_to_try:
        api_key = _get_client(key_type).api_key
        if not is_key_exhausted(api_key):
            valid_models.append((model, key_type))
            
    if not valid_models:
        raise GeminiDailyQuotaExceeded("جميع مفاتيح Gemini المتاحة للتحقق البصري مستنفدة (Exhausted).")

    import time
    last_error = None
    
    # محاولة الحصول على Lock لأي (مفتاح + موديل) متاح
    while True:
        acquired = False
        for model_name, key_type in valid_models:
            api_key = _get_client(key_type).api_key
            if is_key_exhausted(api_key):
                continue
                
            if _try_acquire_key_model_lock(api_key, model_name):
                try:
                    return _verify_media_internal(image_path, narration, model_name, key_type, topic_context)
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    if _is_daily_quota_message(error_str):
                        _mark_key_exhausted(api_key)
                    elif "429" in error_str or "503" in error_str or "quota" in error_str:
                        print(f"[GEMINI CASCADE] الموديل {model_name} غير متاح (429/503). ننتقل لنموذج آخر.")
                    else:
                        print(f"[GEMINI ERROR] خطأ بالموديل {model_name}: {e}.")
                finally:
                    _release_key_model_lock(api_key, model_name)
                    acquired = True
                    break # نجحنا بمحاولة (سواء بنجاح أو بخطأ، نخرج من دورة الـ acquired لنرى إن كنا نحتاج استمرار)
        
        # إذا كل الموديلات كانت محجوزة بواسطة خيوط أخرى بنفس اللحظة
        if not acquired:
            # ننتظر قليلاً ثم نحاول مرة أخرى لضمان عدم حرق الـ CPU
            time.sleep(0.5)
            # تحديث النماذج المتاحة
            valid_models = [(m, k) for m, k in valid_models if not is_key_exhausted(_get_client(k).api_key)]
            if not valid_models:
                raise GeminiDailyQuotaExceeded("جميع مفاتيح Gemini المتاحة تم استنفادها أثناء الانتظار.")
            continue
            
        # إذا وصلنا هنا، يعني إحدى المحاولات تم تنفيذها، ولكن فشلت (last_error).
        # نتأكد إذا تبقى نماذج غير مستنفدة
        valid_models = [(m, k) for m, k in valid_models if not is_key_exhausted(_get_client(k).api_key)]
        if not valid_models:
            break

    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        raise GeminiDailyQuotaExceeded(str(last_error))
    if last_error and not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
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
        
        # فلترة صارمة: إذا كان التطابق الدلالي منخفضاً يُرفض فوراً مهما كانت الجودة.
        passed = score >= 7 and breakdown.get("semantic_match", 0) >= 4
        if not passed and score >= 7:
            print(f"[GEMINI STRICT REJECT] تم رفض الفيديو رغم درجة ({score}) لأن التطابق الدلالي ضعيف جداً ({breakdown.get('semantic_match')}).")
            score = 0.0 # إسقاط الدرجة لضمان عدم اختياره كأفضل مرشح

        return {"score": score, "passed": passed, "breakdown": breakdown, "layer": "gemini", "model": model}


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
        passed = score >= 7 and breakdown.get("semantic_match", 0) >= 4
        if not passed and score >= 7:
            score = 0.0
            
        results[item["id"]] = {
            "score": score, "passed": passed, "breakdown": breakdown,
            "layer": "gemini", "model": model,
        }
    return results


def score_media_relevance_batch(items: list, topic_context: str = "",
                                 only_key_type: str = None) -> dict:
    if not items:
        return {}

    models_to_try = _build_filter_cascade(only_key_type)
    valid_models = [(m, k) for m, k in models_to_try if not is_key_exhausted(_get_client(k).api_key)]
    if not valid_models:
        raise GeminiDailyQuotaExceeded("جميع مفاتيح Gemini للدفعة مستنفدة.")

    import time
    last_error = None
    
    while True:
        acquired = False
        for model_name, key_type in valid_models:
            api_key = _get_client(key_type).api_key
            if is_key_exhausted(api_key):
                continue
                
            if _try_acquire_key_model_lock(api_key, model_name):
                try:
                    _get_verify_rate_limiter(key_type).wait()
                    return _score_media_batch_internal(items, model_name, key_type, topic_context)
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    if _is_daily_quota_message(error_str):
                        _mark_key_exhausted(api_key)
                    elif "429" in error_str or "503" in error_str or "quota" in error_str:
                        pass
                finally:
                    _release_key_model_lock(api_key, model_name)
                    acquired = True
                    break
        
        if not acquired:
            time.sleep(0.5)
            valid_models = [(m, k) for m, k in valid_models if not is_key_exhausted(_get_client(k).api_key)]
            if not valid_models:
                raise GeminiDailyQuotaExceeded("جميع المفاتيح استنفدت للدفعة.")
            continue
            
        valid_models = [(m, k) for m, k in valid_models if not is_key_exhausted(_get_client(k).api_key)]
        if not valid_models:
            break

    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        raise GeminiDailyQuotaExceeded(str(last_error))
    if last_error and not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
        alert_key_error("Gemini Vision (batch)", "filter", str(last_error))
    raise GeminiVerificationUnavailable(str(last_error))


def score_media_relevance(image_path: str, narration: str, topic_context: str = "",
                           only_key_type: str = None) -> dict:
    models_to_try = _build_filter_cascade(only_key_type)
    valid_models = [(m, k) for m, k in models_to_try if not is_key_exhausted(_get_client(k).api_key)]
    if not valid_models:
        raise GeminiDailyQuotaExceeded("جميع مفاتيح Gemini للتقييم مستنفدة.")

    import time
    last_error = None
    
    while True:
        acquired = False
        for model_name, key_type in valid_models:
            api_key = _get_client(key_type).api_key
            if is_key_exhausted(api_key):
                continue
                
            if _try_acquire_key_model_lock(api_key, model_name):
                try:
                    _get_verify_rate_limiter(key_type).wait()
                    return _score_media_internal(image_path, narration, model_name, key_type, topic_context)
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    if _is_daily_quota_message(error_str):
                        _mark_key_exhausted(api_key)
                    elif "429" in error_str or "503" in error_str or "quota" in error_str:
                        pass
                finally:
                    _release_key_model_lock(api_key, model_name)
                    acquired = True
                    break
        
        if not acquired:
            time.sleep(0.5)
            valid_models = [(m, k) for m, k in valid_models if not is_key_exhausted(_get_client(k).api_key)]
            if not valid_models:
                raise GeminiDailyQuotaExceeded("جميع المفاتيح استنفدت للتقييم.")
            continue
            
        valid_models = [(m, k) for m, k in valid_models if not is_key_exhausted(_get_client(k).api_key)]
        if not valid_models:
            break

    last_error_str = str(last_error).lower()
    if _is_daily_quota_message(last_error_str):
        raise GeminiDailyQuotaExceeded(str(last_error))
    if last_error and not ("429" in last_error_str or "503" in last_error_str or "quota" in last_error_str):
        alert_key_error("Gemini Vision", "filter", str(last_error))
    raise GeminiVerificationUnavailable(str(last_error))


@with_backoff(max_retries=3, base_delay=2.0)
def get_embedding(text: str, key_type: str = "light") -> list[float]:
    """يستخدم الموديل الجديد الموحد Gemini Embedding 2."""
    client = _get_client(key_type)
    result = client.models.embed_content(model=MODEL_EMBEDDING_NEW, contents=text)
    return result.embeddings[0].values
