"""
analysis_engine.py
محرك التحليل البصري الموحّد — ملف واحد يُستدعى عند الحاجة لفحص تطابق
أي وسيط (صورة/فيديو) مع نص سردي.

التحديث (تخصيص كل مشهد لعامل مختلف بدل ازدواج نفس المشهد):
--------------------------------------------------------------
سابقاً كانت Gemini و Groq تفحصان نفس المشهد الواحد بالتوازي (ازدواج بلا
فائدة فعلية — نتيجة واحدة فقط تُستخدم). الآن، بما أن جلب/تحليل عدة مشاهد
لنفس الفيديو يحدث أصلاً بالتوازي (كل مشهد بخيط منفصل عبر
parallel_scene_fetcher.py)، أصبح لدينا "عاملان" دائمان (lanes) يعملان طوال
عمر العملية (process) كلها:

  - عامل Gemini: يأخذ أي مشهد لم يُحسم بعد من طابور مشترك (queue) ويحلله
    بنماذج Gemini الخمسة بالتسلسل (راجع gemini_client.score_media_relevance).
  - عامل Groq: بنفس الوقت بالضبط، يأخذ مشهداً آخر (مختلفاً) من نفس الطابور
    ويحلله بنموذج Groq (qwen/qwen3.6-27b).

فكل مشهد يُقرَّر بواسطة عامل واحد فقط (وليس بمقارنة نتيجتين)، لكن — بما أن
العاملين يعملان بنفس الوقت على مشهدين مختلفين — الإنتاجية الفعلية تتضاعف
تقريباً بدل انتظار الأبطأ بينهما على نفس المشهد. بمجرد أن ينتهي عامل من
مشهده، يمسك فوراً المشهد التالي غير المحسوم من الطابور (بغض النظر عن أي
فيديو ينتمي له المشهد)، فطالما هناك مشاهد جديدة تُستدعى verify() عليها من
أي مكان بالمشروع، العاملان يبقيان مشغولين بالتوازي طوال التشغيل.

قواعد التوقف والاستبدال بـ Puter (حسب الطلب بالضبط):
------------------------------------------------------
- لو توقف عامل (Gemini أو Groq) عن العمل مؤقتاً على مشهد معيّن (خطأ عابر:
  حصة دقيقة TPM، ازدحام سيرفر 503، فشل JSON عابر، انقطاع شبكة...) →
  "يتوقف العمل معه لمدة دقيقة على الأقل" حرفياً: هذا العامل تحديداً ينتظر
  60 ثانية على الأقل قبل إعادة محاولة نفس المشهد بنفس النموذج، بينما العامل
  الآخر يستمر بالعمل بلا أي تأثير (لأن كل عامل مستقل تماماً).
- لو تكرر توقف نفس المشهد أكثر من MAX_STALLS_PER_SCENE مرة (خطأ متكرر غير
  متعلق بحصة يومية)، يُسلَّم هذا المشهد بالذات لبقية سلسلة التدرّج
  (Puter → CLIP → رفض) دون التخلي عن العامل نفسه — فيبقى متاحاً
  لأخذ المشاهد التالية من الطابور بشكل طبيعي.
- لو اكتُشف أن حصة العامل استُنفدت يومياً بالكامل (TPD وليس TPM — راجع
  gemini_client.GeminiDailyQuotaExceeded و groq_client.GroqDailyQuotaExceeded)
  → هذا العامل "يتوقف عن العمل في هذا التشغيل" نهائياً (لا فائدة من
  الانتظار، الحصة لن تعود إلا بعد ساعات)، ويصعد Puter AI مكانه فوراً على
  نفس المشهد وكل المشاهد التالية التي يمسكها هذا العامل — بينما العامل
  الآخر (بنموذجه الأصلي) يستمر بالعمل بلا أي تغيير. بمعنى آخر: العاملان
  يبقيان اثنين طوال الوقت، لكن أحدهما فقط قد يتحول من (Gemini/Groq) إلى
  Puter بدل أن يتوقف الإنتاج بالكامل.

سلسلة التدرّج الكاملة (Cascade)، من الأفضل للأسوأ:
  الطبقة 0: فلتر CLIP محلي مسبق (مجاني، بلا حدود طلبات، فوري بعد التحميل
            الأول). يُشغَّل على **كل** مشهد قبل أي استدعاء لـ Gemini/Groq.
            يرفض فقط المشاهد الرديئة الواضحة (تشابه < CLIP_REJECT_THRESHOLD)
            بلا استهلاك أي حصة API. أي مشهد لا يُرفض هنا — حتى لو كان
            تشابهه مرتفعاً جداً بنظر CLIP — يكمل إلزامياً لطبقة 1/2 (Gemini/
            Groq)؛ هذا الفلتر لا يملك صلاحية "قبول نهائي"، فقط "رفض مبكر".
            هذا يقلّل عدد المشاهد التي تستهلك حصة Gemini اليومية دون
            المساس بجودة القرار النهائي.
  الطبقة 1 أو 2 (بحسب أي عامل أمسك المشهد): Gemini Vision (نماذج متعددة
                                             ومفاتيح متعددة، راجع
                                             gemini_client.py) أو
                                             Groq (qwen/qwen3.6-27b).
  الطبقة 3: Puter AI (google/gemini-3.5-flash عبر بنيتهم — حصة منفصلة ثالثة)
            — إما كاستبدال دائم لعامل استنفد حصته اليومية، أو كاحتياط أخير
            لمشهد تعطّل تكراره مع عامله الأصلي.
  الطبقة 4: CLIP المحلي مرة أخرى (كقرار قبول/رفض نهائي بعتبة
            CLIP_SIMILARITY_THRESHOLD) — فقط لو فشلت كل الطبقات 1-3 تماماً.
  (لا توجد طبقة "قبول تلقائي" بعد الآن — حسب الطلب الصريح: أي مشهد لم
   يُحسم بموافقة صريحة من طبقة تحليل حقيقية يُرفض دائماً ولا يمر إطلاقاً.)
"""
import os
import queue
import subprocess
import threading
import time

from scripts import config, gemini_client, groq_client
from scripts.retry_utils import RateLimiter
from scripts.telegram_alerts import send_alert

# عتبة القبول: أي وسيط مجموع نقاطه > 7 من 10 يُعتمد
PASS_THRESHOLD = 7

# الحد الأدنى للانتظار (بالثواني) قبل إعادة محاولة نفس المشهد بنفس العامل
# بعد توقف عابر (وليس حصة يومية) — "يتوقف العمل معه لمدة دقيقة على الأقل"
SCENE_STALL_WAIT_SECONDS = 60.0

# بعد كم توقفاً متتالياً على نفس المشهد بالذات نُسلّمه لبقية سلسلة التدرّج
# (Puter → CLIP → قبول) دون التخلي عن العامل نفسه لبقية المشاهد
MAX_STALLS_PER_SCENE = 3

# فاصل زمني بسيط بين طلبات Puter (قد يستخدمه عاملان بنفس الوقت لو استنفد
# كلا العاملين حصتيهما اليوميتين معاً)
_PUTER_RATE_LIMITER = RateLimiter(min_interval=5.0)

# ═══════════ تعطيل Puter الدائم بعد أول فشل برمجي (TypeError/AttributeError) ═══════════
# لو Puter غير متوافق (خطأ توقيع دالة) أو معطّل (خطأ تسجيل دخول/بيانات ناقصة)،
# نعطّله بالكامل لبقية هذا التشغيل — بلا إعادة محاولة وبلا تنبيهات متكررة.
# يُرسل تنبيه Telegram واحد فقط عند أول اكتشاف.
_puter_state_lock = threading.Lock()
_puter_permanently_disabled = False
_puter_disable_reason = ""
# عدّاد الفشل المتتالي (لأخطاء غير برمجية: شبكة/مصادقة) — بعد PUTER_MAX_CONSECUTIVE_FAILURES
# فشل متتالي يُعطّل Puter أيضاً (بيانات خاطئة/حساب مقفل لن يتعافى بهذا التشغيل).
_puter_consecutive_failures = 0
PUTER_MAX_CONSECUTIVE_FAILURES = 2


def _is_puter_disabled() -> bool:
    with _puter_state_lock:
        return _puter_permanently_disabled


def _disable_puter(reason: str) -> None:
    """يُعطّل Puter بالكامل لبقية هذا التشغيل ويُرسل تنبيه Telegram واحد فقط."""
    global _puter_permanently_disabled, _puter_disable_reason
    with _puter_state_lock:
        if _puter_permanently_disabled:
            return  # سبق تعطيله
        _puter_permanently_disabled = True
        _puter_disable_reason = reason
    print(f"[ANALYSIS] تم تعطيل Puter AI بالكامل لبقية هذا التشغيل: {reason}")
    send_alert(
        f"⚠️ تم تعطيل Puter AI لبقية هذا التشغيل — لن يُحاول مرة أخرى.\n"
        f"السبب: {reason[:400]}\n"
        f"سيُستخدم CLIP المحلي كبديل.",
        level="warning",
        dedup_key="puter_disabled",
    )


def _record_puter_success() -> None:
    """يُصفّر عدّاد الفشل المتتالي عند نجاح Puter."""
    global _puter_consecutive_failures
    with _puter_state_lock:
        _puter_consecutive_failures = 0


def _record_puter_failure(error: Exception) -> None:
    """يُسجّل فشل ويُعطّل Puter لو كان برمجياً أو تجاوز عدد المحاولات."""
    global _puter_consecutive_failures
    # أخطاء برمجية (TypeError/AttributeError/ValueError) = تعطيل فوري
    if isinstance(error, (TypeError, AttributeError, ValueError, ImportError, NameError)):
        _disable_puter(f"خطأ برمجي غير قابل للتعافي ({type(error).__name__}: {error})")
        return
    # أخطاء أخرى (شبكة/مصادقة) = عدّاد متتالي
    with _puter_state_lock:
        _puter_consecutive_failures += 1
        if _puter_consecutive_failures >= PUTER_MAX_CONSECUTIVE_FAILURES:
            _disable_puter(f"فشل {_puter_consecutive_failures} مرات متتالية ({type(error).__name__}: {error})")

# ═══════════════════ تجميع المشاهد بدفعات لطلبات Gemini (حسب الطلب) ═══════════════════
# بدل إرسال طلب Gemini منفصل لكل مشهد على حدة، نجمع عدة مشاهد تنتمي لنفس
# الفيديو (نفس topic_context) ونرسلها بطلب واحد: نُخبر Gemini بموضوع
# الفيديو/الجو العام مرة واحدة، ثم نُرسل له كل مشهد بمعرّفه (اسم الملف)
# ونصه السردي وصورته، ونطلب تقييم كل مشهد على حدة ضمن نفس الرد. هذا يقلّل
# عدد الطلبات المُرسَلة فعلياً (توفير حصة يومية) مع الحفاظ على قرار منفصل
# لكل مشهد بالضبط كما كان سابقاً.
#
# ملاحظة: التجميع هنا خاص بعاملَي Gemini فقط (gemini_a/gemini_b). عامل Groq
# (لا يدعم دفعات بهذا المشروع) يستمر بمعالجة كل مشهد يمسكه من الدفعة على
# حدة تماماً كما كان — فقط استفادة Gemini الفعلية من التجميع تتحقق حين يمسك
# أحد عاملي Gemini الدفعة.
GEMINI_BATCH_SIZE = gemini_client.MAX_BATCH_SCENES_PER_REQUEST  # كم مشهداً كحد أقصى بكل طلب Gemini واحد
GEMINI_BATCH_WAIT_SECONDS = 2.0  # أقصى انتظار لتجميع دفعة (حتى لو لم تكتمل) قبل إرسالها

# --- CLIP configuration (same as was in media_relevance_checker.py) ---
CLIP_SIMILARITY_THRESHOLD = 0.20
# عتبة الرفض المبكر بالطبقة 0 (قبل استدعاء أي Gemini/Groq): أي مشهد تشابهه
# أقل من هذا الرقم يُرفض فوراً محلياً بلا أي استهلاك حصة. أعلى من هذا الرقم
# (حتى لو كان تشابهاً مرتفعاً جداً) يكمل إلزامياً لتحليل Gemini/Groq الكامل —
# هذا الفلتر لا "يقبل" مشهداً أبداً، فقط يرفض الرديء الواضح.
CLIP_REJECT_THRESHOLD = 0.15
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


def _clip_similarity(image_path: str, narration: str, visual_intent: str = "", keywords: list[str] = None) -> float:
    """يحسب درجة التشابه الدلالي الخام (cosine similarity) عبر CLIP محلياً،
    بدون أي قرار قبول/رفض — يُستخدم من طبقتين مختلفتين (الفلتر المسبق L0
    وطبقة الاحتياط الأخير L4) بعتبتين مختلفتين تماماً."""
    import torch
    from PIL import Image
    model, preprocess, tokenizer = _load_clip()
    
    # تحسين CLIP: استخدام الكلمات المفتاحية والنية البصرية بدل النص السردي الطويل
    if keywords:
        text = ", ".join(keywords)
        if visual_intent:
            text = f"{visual_intent}: {text}"
    else:
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
    return similarity


def _clip_check(image_path: str, narration: str, visual_intent: str = "", keywords: list[str] = None) -> dict:
    """فحص قبول/رفض نهائي عبر CLIP — يُستخدم فقط بالطبقة 4 (احتياط أخير
    بعد فشل Gemini/Groq/Puter بالكامل)."""
    similarity = _clip_similarity(image_path, narration, visual_intent, keywords)
    print(f"[CLIP] درجة التشابه: {similarity:.3f} (عتبة القبول: {CLIP_SIMILARITY_THRESHOLD})")
    passed = similarity >= CLIP_SIMILARITY_THRESHOLD
    # نعيد قاموساً متوافقاً مع شكل نتيجة Gemini/Groq
    return {"passed": passed, "score": min(10.0, max(0.0, similarity * 10.0)), "breakdown": {}, "layer": "clip", "model": CLIP_MODEL_NAME}


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
    """يتحقق إذا كان الخطأ متعلقاً بالحصة (429/503/rate limit) — تستخدم
    فقط لتقرير ما إذا نرسل تنبيه تليقرام أم لا، وليس لتمييز TPD عن TPM."""
    err = str(error).lower()
    return "429" in err or "503" in err or "quota" in err or "rate" in err


# ═══════════════════════ حالة العاملين الدائمين (lanes) ═══════════════════════
# كل عامل (lane) خيط دائم يعمل طوال عمر العملية، يمسك مشهداً تلو الآخر من
# طابور مشترك.
#
# lane_name -> (family, gemini_key_type):
#   "gemini_a" -> ("gemini", "filter")   دائماً موجود
#   "gemini_b" -> ("gemini", "filter2")  فقط لو GEMINI_KEY_FILTER_2 معرَّف
#                 بالأسرار — عامل مستقل بمفتاح Gemini ثانٍ منفصل تماماً،
#                 يمسك مشاهد مختلفة بنفس الوقت (وليس مجرد احتياط تسلسلي
#                 للمفتاح الأول كما كان سابقاً). هذا يوزّع فعلياً المشاهد
#                 بين مفتاحي Gemini بدل تكديسها على مفتاح واحد.
#   "groq"    -> ("groq", None)
def _build_lane_names() -> tuple:
    lanes = ["gemini_a"]
    if config.GEMINI_KEY_FILTER_2:
        lanes.append("gemini_b")
    lanes.append("groq")
    return tuple(lanes)


_LANE_NAMES = _build_lane_names()
_LANE_GEMINI_KEY = {"gemini_a": "filter", "gemini_b": "filter2"}

_lane_state_lock = threading.Lock()
_lane_exhausted = {name: False for name in _LANE_NAMES}  # استُنفدت حصته اليومية بالكامل؟

_dispatch_queue: "queue.Queue" = queue.Queue()
_lanes_started = False
_lanes_lock = threading.Lock()


_batch_id_lock = threading.Lock()
_batch_id_counter = 0


def _next_scene_id(file_path: str) -> str:
    """معرّف فريد للمشهد يُرسَل لـ Gemini ضمن الدفعة — نستخدم اسم الملف
    نفسه (حسب الطلب: 'المشهد الأول الذي يحمل اسم اسم الملف') مع رقم تسلسلي
    مُضاف لضمان عدم تكرار نفس الاسم مرتين بنفس الدفعة (لو تصادف نفس اسم
    الملف لمشهدين مختلفين)."""
    global _batch_id_counter
    base = os.path.basename(file_path)
    with _batch_id_lock:
        _batch_id_counter += 1
        n = _batch_id_counter
    return f"{base}#{n}"


class _VerifyJob:
    """طلب فحص مشهد واحد بانتظار أي عامل متاح يلتقطه من الطابور (ضمن دفعة
    مع مشاهد أخرى لو التقطه أحد عاملي Gemini). يحمل الإطار المُستخرَج مسبقاً
    (check_path/is_temp) لأن verify() يستخرجه مرة واحدة فقط لاستخدامه أولاً
    بفلتر CLIP L0 ثم بالعامل الذي يمسك المشهد — بدل استخراجه مرتين.
    topic_context: المزاج/الهوية البصرية الثابتة لكامل الفيديو، تُمرَّر
    دائماً كمتغير ثابت مع كل مشهد بدل الاعتماد فقط على كلمة المشهد
    المفتاحية، وهي أيضاً مفتاح تجميع الدفعة (مشاهد نفس الفيديو فقط تُجمَّع
    ببعضها). id: معرّف المشهد (اسم ملفه) المُرسَل لـ Gemini ضمن الدفعة."""
    __slots__ = ("id", "file_path", "narration", "check_path", "is_temp", "topic_context", "visual_intent", "keywords", "event", "result")

    def __init__(self, file_path: str, narration: str, check_path, is_temp: bool, topic_context: str = "", visual_intent: str = "", keywords: list[str] = None):
        self.id = _next_scene_id(file_path)
        self.file_path = file_path
        self.narration = narration
        self.check_path = check_path
        self.is_temp = is_temp
        self.topic_context = topic_context
        self.visual_intent = visual_intent
        self.keywords = keywords
        self.event = threading.Event()
        self.result = {"passed": False, "score": 0.0, "breakdown": {}, "layer": "none", "model": "none"}



class _BatchJob:
    """دفعة من عدة _VerifyJob تنتمي لنفس topic_context (نفس الفيديو)، تُحل
    دفعة واحدة بطلب Gemini واحد لو التقطها عامل Gemini، أو مشهداً مشهداً
    بالطريقة القديمة لو التقطها عامل Groq (لا يدعم الدفعات)."""
    __slots__ = ("jobs", "topic_context")

    def __init__(self, jobs: list, topic_context: str):
        self.jobs = jobs
        self.topic_context = topic_context


_batch_lock = threading.Lock()
_pending_batches: dict = {}   # topic_context -> list[_VerifyJob] بانتظار الاكتمال/الإرسال
_batch_timers: dict = {}      # topic_context -> threading.Timer (يُرسل الدفعة حتى لو لم تكتمل)


def _flush_batch(topic_context: str) -> None:
    """يسحب كل مشاهد مجموعة topic_context الحالية (لو وُجدت) ويضعها كدفعة
    واحدة على الطابور المشترك، ويُلغي مؤقّت الانتظار المرتبط بها إن وُجد."""
    with _batch_lock:
        jobs = _pending_batches.pop(topic_context, None)
        timer = _batch_timers.pop(topic_context, None)
    if timer:
        timer.cancel()
    if jobs:
        _dispatch_queue.put(_BatchJob(jobs, topic_context))


def _enqueue_for_batch(job: "_VerifyJob") -> None:
    """يضيف مشهداً لمجموعة انتظار topic_context الخاصة به بدل إرساله فوراً
    بمفرده. يُرسِل الدفعة فوراً لو اكتملت (GEMINI_BATCH_SIZE)، وإلا يبدأ (أو
    يُبقي) مؤقّتاً يضمن عدم انتظار مشهد وحيد لأجل غير مسمى لو لم تكتمل
    الدفعة أبداً (فيديو بمشاهد قليلة، أو نهاية قائمة المشاهد)."""
    key = job.topic_context or ""
    ready_batch = None
    with _batch_lock:
        group = _pending_batches.setdefault(key, [])
        group.append(job)
        if len(group) >= GEMINI_BATCH_SIZE:
            ready_batch = group
            _pending_batches[key] = []
            old_timer = _batch_timers.pop(key, None)
        else:
            old_timer = None
            if key not in _batch_timers:
                timer = threading.Timer(GEMINI_BATCH_WAIT_SECONDS, _flush_batch, args=(key,))
                timer.daemon = True
                _batch_timers[key] = timer
                timer.start()
    if old_timer:
        old_timer.cancel()
    if ready_batch:
        _dispatch_queue.put(_BatchJob(ready_batch, key))


def _lane_is_exhausted(lane_name: str) -> bool:
    with _lane_state_lock:
        return _lane_exhausted[lane_name]


def _mark_lane_exhausted(lane_name: str) -> None:
    with _lane_state_lock:
        if not _lane_exhausted[lane_name]:
            _lane_exhausted[lane_name] = True
            print(f"[ANALYSIS] عامل {lane_name} استنفد حصته اليومية بالكامل — "
                  f"سيتوقف عن العمل بنموذجه الأصلي لبقية هذا التشغيل، ويحل Puter مكانه فوراً.")


def _score_with_puter(file_path: str, narration: str, topic_context: str = "") -> dict:
    """Puter لا يملك نظام تقييم /10 (فقط YES/NO) — نغلّفه بنفس شكل قاموس
    النتيجة المستخدم في gemini_client/groq_client حتى تتعامل بقية الكود
    معه بنفس الطريقة.
    
    يُعطّل Puter فوراً لو كان الخطأ برمجياً أو فشل متتالياً."""
    # فحص سريع: هل Puter معطّل بالفعل؟
    if _is_puter_disabled():
        from scripts.puter_client import PuterVerificationUnavailable
        raise PuterVerificationUnavailable(
            f"Puter معطّل لبقية هذا التشغيل: {_puter_disable_reason}"
        )
    from scripts import puter_client
    _PUTER_RATE_LIMITER.wait()
    try:
        ok = puter_client.verify_media_relevance(file_path, narration, topic_context)
        _record_puter_success()
        return {"score": 10.0 if ok else 0.0, "passed": ok, "breakdown": {}, "layer": "puter", "model": "puter/google-gemini-3.5-flash"}
    except Exception as e:
        _record_puter_failure(e)
        raise


def _fallback_cascade(file_path: str, narration: str, check_path, is_temp: bool,
                       skip_puter: bool = False, topic_context: str = "", visual_intent: str = "", keywords: list[str] = None) -> dict:
    """احتياط أخير لمشهد بعينه فقط (لا يُغيّر حالة أي عامل): Puter → CLIP
    → رفض تلقائي. نفس منطق الطبقات 3-4 بالتصميم الأصلي، لكن مُطبَّق على
    مشهد واحد متعطّل بدل كل التحليل.

    قاعدة صارمة (حسب الطلب): أي مشهد لم يُحسم فعلياً بموافقة صريحة من طبقة
    تحليل حقيقية (Puter أو CLIP بعتبته) لا يمر أبداً مهما حدث — لا يوجد
    'قبول تلقائي' كملاذ أخير بعد الآن. فشل كل الطبقات = رفض المشهد."""
    if not skip_puter and not _is_puter_disabled():
        try:
            result = _score_with_puter(file_path, narration, topic_context)
            if not result["passed"]:
                print(f"[ANALYSIS L3] Puter رفض الوسيط: {narration[:50]}...")
            return result
        except Exception as e:
            puter_err_name = type(e).__name__
            # _record_puter_failure سبق استدعاؤها داخل _score_with_puter
            # — هنا نُسجّل فقط بالـ log بلا تنبيه مكرر
            print(f"[ANALYSIS L3] Puter غير متاح ({puter_err_name}: {e}). الانتقال لـ CLIP...")
    elif _is_puter_disabled():
        print(f"[ANALYSIS L3] Puter معطّل لبقية هذا التشغيل. تخطّي فوري إلى CLIP.")

    if check_path is not None:
        try:
            result = _clip_check(check_path, narration, visual_intent, keywords)
            if not result["passed"]:
                print(f"[ANALYSIS L4] CLIP رفض الوسيط: {narration[:50]}...")
            return result
        except Exception as e:
            print(f"[ANALYSIS L4] CLIP فشل أيضاً ({e}). رفض تلقائي — لا يوجد قبول أعمى.")

    send_alert(
        "⚠️ فشلت جميع طبقات التحليل البصري لهذا المشهد (النموذج المخصص له → Puter → CLIP). "
        "تم رفض الوسيط تلقائياً (لا يمر أي مشهد لم يُحسم بموافقة صريحة).",
        level="warning",
    )
    return {"passed": False, "score": 0.0, "breakdown": {}, "layer": "none", "model": "none"}


def _resolve_job(lane_name: str, file_path: str, narration: str, check_path, is_temp: bool,
                  topic_context: str = "", visual_intent: str = "", keywords: list[str] = None) -> dict:
    """يحل مصير مشهد واحد بعامل معيّن (lane_name)، بما فيه كل منطق
    التوقف/الانتظار/الاستبدال بـ Puter الموضّح بأعلى الملف. check_path/is_temp
    مُستخرَجان مسبقاً من verify() (بعد أن اجتاز المشهد فلتر CLIP L0).

    قاعدة صارمة: تعذّر استخراج إطار للتحليل يعني عدم إمكانية تحليل المشهد
    إطلاقاً — لا يُعامَل كقبول تلقائي (حسب الطلب: أي مشهد لا يتم تحليله
    لمطابقة المشهد لا يمر أبداً مهما حدث)."""
    if check_path is None:
        print("[ANALYSIS] تعذر استخراج إطار من الفيديو — تعذّر التحليل. رفض المشهد (لا يمر بلا تحليل).")
        return {"passed": False, "score": 0.0, "breakdown": {}, "layer": "none", "model": "none"}

    lane_family = "gemini" if lane_name.startswith("gemini") else lane_name  # "gemini_a"/"gemini_b" -> "gemini"، "groq" -> "groq"
    gemini_key_type = _LANE_GEMINI_KEY.get(lane_name)  # "filter" أو "filter2" لو عامل Gemini

    try:
        active_model = "puter" if _lane_is_exhausted(lane_name) else lane_family
        stalls = 0
        while True:
            try:
                if active_model == "gemini":
                    result = gemini_client.score_media_relevance(
                        check_path, narration, topic_context=topic_context, only_key_type=gemini_key_type
                    )
                elif active_model == "groq":
                    result = groq_client.score_media_relevance(file_path, narration, topic_context=topic_context)
                else:  # puter — إما استبدال دائم لعامل مستنفَد، أو محاولة احتياط لمشهد متعطّل
                    result = _score_with_puter(file_path, narration, topic_context)

                print(f"[ANALYSIS {lane_name.upper()}/{active_model.upper()}] قيّم المشهد بـ {result['score']}/10 "
                      f"({result.get('breakdown')}) لـ: {narration[:50]}...")
                return result

            except (gemini_client.GeminiDailyQuotaExceeded, groq_client.GroqDailyQuotaExceeded):
                # حصة يومية (TPD/RPD) — لا فائدة من انتظار دقيقة، النموذج لن يعود
                # إلا بعد ساعات. العامل يتوقف عن العمل بنموذجه الأصلي لبقية هذا
                # التشغيل، ويحل Puter مكانه فوراً على نفس هذا المشهد وكل ما يليه.
                _mark_lane_exhausted(lane_name)
                if active_model == "puter":
                    # احتياط نظري فقط (Puter لا يرفع هذه الاستثناءات أصلاً)
                    return _fallback_cascade(file_path, narration, check_path, is_temp,
                                              skip_puter=True, topic_context=topic_context, visual_intent=visual_intent, keywords=keywords)
                active_model = "puter"
                stalls = 0
                continue

            except Exception as e:
                # إصلاح: أخطاء برمجية بحتة (TypeError/AttributeError من استدعاء
                # دالة بشكل خاطئ، وليس عطلاً عابراً بالشبكة/النموذج) لن تُحل
                # أبداً بالانتظار — سابقاً كنا نعاملها كـ"توقف عابر" فننتظر
                # 60 ثانية × 3 محاولات (3 دقائق كاملة مهدورة لكل مشهد) قبل
                # التسليم لبقية السلسلة، رغم أن النتيجة معروفة مسبقاً بأنها
                # ستفشل بنفس الطريقة تماماً في كل مرة. الآن نتعرّف عليها
                # ونسلّم المشهد فوراً بلا انتظار.
                if isinstance(e, (TypeError, AttributeError, NameError, ValueError)):
                    print(f"[ANALYSIS] خطأ برمجي غير قابل للتعافي بالانتظار في عامل {active_model} "
                          f"({type(e).__name__}: {e}). تسليم فوري لبقية سلسلة التدرّج بلا انتظار.")
                    return _fallback_cascade(file_path, narration, check_path, is_temp,
                                              skip_puter=(active_model == "puter"), topic_context=topic_context, visual_intent=visual_intent, keywords=keywords)

                stalls += 1
                if stalls > MAX_STALLS_PER_SCENE:
                    print(f"[ANALYSIS] {active_model} توقف {stalls} مرات متتالية على نفس هذا المشهد. "
                          f"تسليمه لبقية سلسلة التدرّج (Puter→CLIP→رفض) دون التخلي عن هذا العامل لبقية المشاهد.")
                    return _fallback_cascade(file_path, narration, check_path, is_temp,
                                              skip_puter=(active_model == "puter"), topic_context=topic_context, visual_intent=visual_intent, keywords=keywords)
                print(f"[ANALYSIS] عامل {active_model} توقف مؤقتاً على هذا المشهد ({e}). "
                      f"الانتظار {SCENE_STALL_WAIT_SECONDS:.0f}s على الأقل قبل إعادة المحاولة "
                      f"(محاولة {stalls}/{MAX_STALLS_PER_SCENE})...")
                time.sleep(SCENE_STALL_WAIT_SECONDS)
                continue
    finally:
        if is_temp and check_path and os.path.exists(check_path):
            try:
                os.remove(check_path)
            except Exception:
                pass


def _resolve_batch_individually(lane_name: str, jobs: list) -> None:
    """يحل كل مشاهد الدفعة مشهداً مشهداً بالطريقة القديمة (نفس _resolve_job)
    — تُستخدم من عامل Groq (لا يدعم دفعات) ومن عامل Gemini كاحتياط لو فشل
    طلب الدفعة الموحّد بالكامل."""
    for job in jobs:
        try:
            job.result = _resolve_job(lane_name, job.file_path, job.narration, job.check_path,
                                       job.is_temp, job.topic_context, job.visual_intent, job.keywords)
        except Exception as e:
            print(f"[ANALYSIS] خطأ غير متوقع بعامل {lane_name} على مشهد ضمن دفعة: {e}. "
                  f"رفض هذا المشهد (لا يمر أي مشهد لم يُحسم بموافقة صريحة).")
            job.result = {"passed": False, "score": 0.0, "breakdown": {}, "layer": "none", "model": "none"}
        finally:
            job.event.set()


def _resolve_batch_gemini(lane_name: str, jobs: list, topic_context: str) -> None:
    """يحل دفعة كاملة (حتى GEMINI_BATCH_SIZE مشهداً) بطلب Gemini واحد فقط،
    حسب الطلب: نُخبر Gemini بسياق الفيديو العام مرة واحدة، ونرسل له كل
    مشهد بمعرّفه (اسم الملف) ونصه السردي وصورته، فيرجع تقييم كل مشهد على
    حدة ضمن نفس الرد.

    قواعد التوقف/الاستبدال نفسها المطبَّقة على المشهد المفرد بـ _resolve_job
    (راجع رأس الملف)، لكن على مستوى الدفعة بأكملها: توقف عابر → إعادة
    محاولة الدفعة كاملة بعد الانتظار، حصة يومية → تحويل هذا العامل لـ Puter
    نهائياً لبقية التشغيل وتسليم هذه الدفعة تحديداً لبقية سلسلة التدرّج
    مشهداً مشهداً (Puter لا يدعم دفعات)، تعطّل متكرر لنفس الدفعة بالذات →
    تسليمها لبقية سلسلة التدرّج مشهداً مشهداً دون التخلي عن العامل."""
    gemini_key_type = _LANE_GEMINI_KEY.get(lane_name)
    items = [{"id": job.id, "path": job.check_path, "narration": job.narration} for job in jobs]

    if _lane_is_exhausted(lane_name):
        # هذا العامل مُستبدَل بـ Puter بالكامل بالفعل — Puter لا يدعم دفعات،
        # فنحل كل مشهد على حدة (يبقى استبدالاً دائماً وليس فردياً هنا).
        _resolve_batch_individually(lane_name, jobs)
        return

    stalls = 0
    while True:
        try:
            results = gemini_client.score_media_relevance_batch(items, topic_context=topic_context,
                                                                  only_key_type=gemini_key_type)
            for job in jobs:
                entry = results.get(job.id)
                if entry is None:
                    # نادراً: رجع Gemini دفعة ناقصة معرّف واحد أو أكثر — نحل
                    # هذا المشهد بالذات فردياً بدل تخمين نتيجته.
                    print(f"[ANALYSIS {lane_name.upper()}/GEMINI-BATCH] لم يرجع تقييم للمعرّف '{job.id}' "
                          f"ضمن رد الدفعة. حل هذا المشهد فردياً كاحتياط.")
                    job.result = _resolve_job(lane_name, job.file_path, job.narration, job.check_path,
                                               job.is_temp, job.topic_context, job.visual_intent, job.keywords)
                else:
                    print(f"[ANALYSIS {lane_name.upper()}/GEMINI-BATCH] قيّم المشهد '{job.id}' بـ "
                          f"{entry['score']}/10 ({entry.get('breakdown')}) لـ: {job.narration[:50]}...")
                    job.result = entry
                job.event.set()
            return

        except gemini_client.GeminiDailyQuotaExceeded:
            _mark_lane_exhausted(lane_name)
            print(f"[ANALYSIS {lane_name.upper()}] استُنفدت الحصة اليومية أثناء معالجة دفعة من {len(jobs)} "
                  f"مشهداً. تسليم هذه الدفعة لـ Puter/CLIP مشهداً مشهداً (Puter لا يدعم دفعات).")
            _resolve_batch_individually(lane_name, jobs)
            return

        except Exception as e:
            if isinstance(e, (TypeError, AttributeError, NameError, ValueError)):
                print(f"[ANALYSIS {lane_name.upper()}] خطأ برمجي غير قابل للتعافي بالانتظار أثناء دفعة "
                      f"({type(e).__name__}: {e}). تسليم الدفعة فوراً لبقية سلسلة التدرّج مشهداً مشهداً.")
                _resolve_batch_individually(lane_name, jobs)
                return

            stalls += 1
            if stalls > MAX_STALLS_PER_SCENE:
                print(f"[ANALYSIS {lane_name.upper()}] توقف {stalls} مرات متتالية على نفس هذه الدفعة "
                      f"({len(jobs)} مشهداً). تسليمها لبقية سلسلة التدرّج مشهداً مشهداً.")
                _resolve_batch_individually(lane_name, jobs)
                return
            print(f"[ANALYSIS {lane_name.upper()}] توقف مؤقت على دفعة من {len(jobs)} مشهداً ({e}). "
                  f"الانتظار {SCENE_STALL_WAIT_SECONDS:.0f}s قبل إعادة محاولة نفس الدفعة كاملة "
                  f"(محاولة {stalls}/{MAX_STALLS_PER_SCENE})...")
            time.sleep(SCENE_STALL_WAIT_SECONDS)
            continue


def _lane_worker(lane_name: str) -> None:
    """خيط دائم واحد لكل عامل — يمسك دفعة (أو مشهداً واحداً ضمن دفعة من
    عنصر واحد) تلو الأخرى من الطابور المشترك طوال عمر العملية، بالتوازي
    التام مع بقية العمال. عاملا Gemini يحلّان الدفعة بطلب واحد؛ عامل Groq
    (وأي عامل مُستبدَل بـ Puter) يحلّها مشهداً مشهداً كما كان سابقاً."""
    lane_family = "gemini" if lane_name.startswith("gemini") else lane_name
    while True:
        batch: _BatchJob = _dispatch_queue.get()
        try:
            if lane_family == "gemini":
                _resolve_batch_gemini(lane_name, batch.jobs, batch.topic_context)
            else:
                _resolve_batch_individually(lane_name, batch.jobs)
        except Exception as e:
            print(f"[ANALYSIS] خطأ غير متوقع بعامل {lane_name} أثناء معالجة دفعة: {e}. "
                  f"رفض كل مشاهد هذه الدفعة (لا يمر أي مشهد لم يُحسم بموافقة صريحة).")
            for job in batch.jobs:
                job.result = {"passed": False, "score": 0.0, "breakdown": {}, "layer": "none", "model": "none"}
                job.event.set()
        finally:
            _dispatch_queue.task_done()  # عنصر واحد فقط سُحب من الطابور (الدفعة كاملة)


def _ensure_lanes_started() -> None:
    global _lanes_started
    if _lanes_started:
        return
    with _lanes_lock:
        if _lanes_started:
            return
        for lane_name in _LANE_NAMES:
            threading.Thread(target=_lane_worker, args=(lane_name,), daemon=True,
                              name=f"analysis-lane-{lane_name}").start()
        _lanes_started = True
        print(f"[ANALYSIS] عمال التحليل المتوازي بدأوا: {', '.join(_LANE_NAMES)}")


def verify(file_path: str, narration: str, topic_context: str = "", visual_intent: str = "", keywords: list[str] = None) -> dict:
    """
    الدالة الرئيسية الوحيدة — تُستدعى من أي ملف يحتاج فحص تطابق بصري.

    topic_context: المزاج/الهوية البصرية الثابتة لكامل الفيديو (اختياري،
    قيمته الافتراضية "" للتوافق الخلفي مع أي مستدعٍ قديم لا يمرره). يُمرَّر
    كمتغير ثابت مع كل مشهد إلى كل طبقات التحليل (راجع الملاحظة أعلى الملف).

    الآن تبدأ بفلتر CLIP محلي مجاني (الطبقة 0): لو كان تشابه المشهد مع
    السرد أقل من CLIP_REJECT_THRESHOLD بوضوح، يُرفض المشهد فوراً محلياً
    بلا أي استهلاك لحصة Gemini/Groq اليومية. أي مشهد لا يُرفض هنا — حتى لو
    كان تشابهه مرتفعاً — يُرسَل إلزامياً لطابور Gemini/Groq المشترك تماماً
    كما كان سابقاً؛ هذا الفلتر المسبق لا يملك صلاحية قبول مشهد بمفرده أبداً.

    داخلياً (بعد الفلتر): يُضاف هذا المشهد لمجموعة انتظار خاصة بـ topic_context
    (أي: مشاهد نفس الفيديو تُجمَّع ببعضها فقط) بدل إرساله فوراً بمفرده — حسب
    الطلب: بدل طلب Gemini منفصل لكل مشهد، تُرسَل دفعة من عدة مشاهد (حتى
    GEMINI_BATCH_SIZE) بطلب واحد، نُخبر فيه Gemini بموضوع الفيديو/الجو العام
    مرة واحدة ثم نُرسل كل مشهد بمعرّفه (اسم الملف) ونصه السردي وصورته. تُرسَل
    الدفعة فوراً لو اكتملت، أو بعد GEMINI_BATCH_WAIT_SECONDS كحد أقصى انتظار
    لو لم تكتمل (فيديو بمشاهد قليلة). بمجرد إرسال الدفعة، يعمل عليها أي عامل
    دائم متاح (مفتاح/مفاتيح Gemini + Groq) بالتوازي طوال التشغيل. تُحظر
    (block) حتى يُحسم مشهدها تحديداً ثم ترجع dict.

    قاعدة صارمة: لو تعذّر استخراج إطار للتحليل من الأساس، لا يمكن تحليل
    المشهد إطلاقاً → يُرفض (لا يوجد قبول تلقائي لمشهد لم يُحلَّل)."""
    check_path, is_temp = _extract_frame_for_check(file_path)
    if check_path is None:
        print("[ANALYSIS] تعذر استخراج إطار من الفيديو — تعذّر التحليل. رفض المشهد (لا يمر بلا تحليل).")
        return {"passed": False, "score": 0.0, "breakdown": {}, "layer": "none", "model": "none"}

    # --- الطبقة 0: فلتر CLIP محلي مسبق (رفض فقط، بلا استهلاك حصة) ---
    try:
        similarity = _clip_similarity(check_path, narration, visual_intent, keywords)
        print(f"[CLIP L0] فلتر مسبق قبل Gemini/Groq: تشابه {similarity:.3f} "
              f"(عتبة الرفض المبكر: {CLIP_REJECT_THRESHOLD})")
        if similarity < CLIP_REJECT_THRESHOLD:
            print(f"[CLIP L0] رفض مبكر — لن يُستهلك أي حصة Gemini/Groq لهذا المشهد: {narration[:50]}...")
            if is_temp and os.path.exists(check_path):
                try:
                    os.remove(check_path)
                except Exception:
                    pass
            return {"passed": False, "score": min(10.0, max(0.0, similarity * 10.0)), "breakdown": {}, "layer": "clip_l0", "model": CLIP_MODEL_NAME}
    except Exception as e:
        print(f"[CLIP L0] تعذر تشغيل الفلتر المسبق ({e}). المتابعة مباشرة لتحليل Gemini/Groq الكامل.")

    _ensure_lanes_started()
    job = _VerifyJob(file_path, narration, check_path, is_temp, topic_context, visual_intent, keywords)
    _enqueue_for_batch(job)
    job.event.wait()
    return job.result

