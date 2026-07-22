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
  (Puter → CLIP → قبول تلقائي) دون التخلي عن العامل نفسه — فيبقى متاحاً
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
  الطبقة 5: قبول تلقائي (فقط لو تعطل كل شيء — لمنع توقف الإنتاج)
"""
import os
import queue
import subprocess
import threading
import time

from scripts import gemini_client, groq_client
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


def _clip_similarity(image_path: str, narration: str) -> float:
    """يحسب درجة التشابه الدلالي الخام (cosine similarity) عبر CLIP محلياً،
    بدون أي قرار قبول/رفض — يُستخدم من طبقتين مختلفتين (الفلتر المسبق L0
    وطبقة الاحتياط الأخير L4) بعتبتين مختلفتين تماماً."""
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
    return similarity


def _clip_check(image_path: str, narration: str) -> bool:
    """فحص قبول/رفض نهائي عبر CLIP — يُستخدم فقط بالطبقة 4 (احتياط أخير
    بعد فشل Gemini/Groq/Puter بالكامل)."""
    similarity = _clip_similarity(image_path, narration)
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
    """يتحقق إذا كان الخطأ متعلقاً بالحصة (429/503/rate limit) — تستخدم
    فقط لتقرير ما إذا نرسل تنبيه تليقرام أم لا، وليس لتمييز TPD عن TPM."""
    err = str(error).lower()
    return "429" in err or "503" in err or "quota" in err or "rate" in err


# ═══════════════════════ حالة العاملين الدائمين (lanes) ═══════════════════════
# كل عامل (lane) خيط دائم يعمل طوال عمر العملية، يمسك مشهداً تلو الآخر من
# طابور مشترك. lane_name هو أيضاً اسم النموذج الأصلي المخصص له.
_LANE_NAMES = ("gemini", "groq")

_lane_state_lock = threading.Lock()
_lane_exhausted = {name: False for name in _LANE_NAMES}  # استُنفدت حصته اليومية بالكامل؟

_dispatch_queue: "queue.Queue" = queue.Queue()
_lanes_started = False
_lanes_lock = threading.Lock()


class _VerifyJob:
    """طلب فحص مشهد واحد بانتظار أي عامل متاح يلتقطه من الطابور. يحمل
    الإطار المُستخرَج مسبقاً (check_path/is_temp) لأن verify() يستخرجه
    مرة واحدة فقط لاستخدامه أولاً بفلتر CLIP L0 ثم بالعامل الذي يمسك
    المشهد — بدل استخراجه مرتين."""
    __slots__ = ("file_path", "narration", "check_path", "is_temp", "event", "result")

    def __init__(self, file_path: str, narration: str, check_path, is_temp: bool):
        self.file_path = file_path
        self.narration = narration
        self.check_path = check_path
        self.is_temp = is_temp
        self.event = threading.Event()
        self.result = True  # افتراضي آمن لو حصل خطأ غير متوقع تماماً بالعامل


def _lane_is_exhausted(lane_name: str) -> bool:
    with _lane_state_lock:
        return _lane_exhausted[lane_name]


def _mark_lane_exhausted(lane_name: str) -> None:
    with _lane_state_lock:
        if not _lane_exhausted[lane_name]:
            _lane_exhausted[lane_name] = True
            print(f"[ANALYSIS] عامل {lane_name} استنفد حصته اليومية بالكامل — "
                  f"سيتوقف عن العمل بنموذجه الأصلي لبقية هذا التشغيل، ويحل Puter مكانه فوراً.")


def _score_with_puter(file_path: str, narration: str) -> dict:
    """Puter لا يملك نظام تقييم /10 (فقط YES/NO) — نغلّفه بنفس شكل قاموس
    النتيجة المستخدم في gemini_client/groq_client حتى تتعامل بقية الكود
    معه بنفس الطريقة."""
    from scripts import puter_client
    _PUTER_RATE_LIMITER.wait()
    ok = puter_client.verify_media_relevance(file_path, narration)
    return {"score": 10.0 if ok else 0.0, "passed": ok, "breakdown": {}, "layer": "puter", "model": "puter/google-gemini-3.5-flash"}


def _fallback_cascade(file_path: str, narration: str, check_path, is_temp: bool, skip_puter: bool = False) -> bool:
    """احتياط أخير لمشهد بعينه فقط (لا يُغيّر حالة أي عامل): Puter → CLIP
    → قبول تلقائي. نفس منطق الطبقات 3-5 بالتصميم الأصلي، لكن مُطبَّق على
    مشهد واحد متعطّل بدل كل التحليل."""
    if not skip_puter:
        try:
            result = _score_with_puter(file_path, narration)
            if not result["passed"]:
                print(f"[ANALYSIS L3] Puter رفض الوسيط: {narration[:50]}...")
            return result["passed"]
        except Exception as e:
            puter_err_name = type(e).__name__
            if not _is_quota_error(e):
                from scripts.telegram_alerts import alert_key_error
                alert_key_error("Puter AI", "PUTER_USERNAME", str(e))
            print(f"[ANALYSIS L3] Puter غير متاح ({puter_err_name}: {e}). الانتقال لـ CLIP...")

    if check_path is not None:
        try:
            result = _clip_check(check_path, narration)
            if not result:
                print(f"[ANALYSIS L4] CLIP رفض الوسيط: {narration[:50]}...")
            return result
        except Exception as e:
            print(f"[ANALYSIS L4] CLIP فشل أيضاً ({e}). قبول تلقائي كملاذ أخير.")

    send_alert(
        "⚠️ فشلت جميع طبقات التحليل البصري لهذا المشهد (النموذج المخصص له → Puter → CLIP). "
        "تم قبول الوسيط تلقائياً لمنع توقف الإنتاج.",
        level="warning",
    )
    return True


def _resolve_job(lane_name: str, file_path: str, narration: str, check_path, is_temp: bool) -> bool:
    """يحل مصير مشهد واحد بعامل معيّن (lane_name)، بما فيه كل منطق
    التوقف/الانتظار/الاستبدال بـ Puter الموضّح بأعلى الملف. check_path/is_temp
    مُستخرَجان مسبقاً من verify() (بعد أن اجتاز المشهد فلتر CLIP L0)."""
    if check_path is None:
        print("[ANALYSIS] تعذر استخراج إطار من الفيديو. قبول تلقائي.")
        return True

    try:
        active_model = "puter" if _lane_is_exhausted(lane_name) else lane_name
        stalls = 0
        while True:
            try:
                if active_model == "gemini":
                    result = gemini_client.score_media_relevance(check_path, narration)
                elif active_model == "groq":
                    result = groq_client.score_media_relevance(file_path, narration)
                else:  # puter — إما استبدال دائم لعامل مستنفَد، أو محاولة احتياط لمشهد متعطّل
                    result = _score_with_puter(file_path, narration)

                print(f"[ANALYSIS {active_model.upper()}] قيّم المشهد بـ {result['score']}/10 "
                      f"({result.get('breakdown')}) لـ: {narration[:50]}...")
                return result["passed"]

            except (gemini_client.GeminiDailyQuotaExceeded, groq_client.GroqDailyQuotaExceeded):
                # حصة يومية (TPD/RPD) — لا فائدة من انتظار دقيقة، النموذج لن يعود
                # إلا بعد ساعات. العامل يتوقف عن العمل بنموذجه الأصلي لبقية هذا
                # التشغيل، ويحل Puter مكانه فوراً على نفس هذا المشهد وكل ما يليه.
                _mark_lane_exhausted(lane_name)
                if active_model == "puter":
                    # احتياط نظري فقط (Puter لا يرفع هذه الاستثناءات أصلاً)
                    return _fallback_cascade(file_path, narration, check_path, is_temp, skip_puter=True)
                active_model = "puter"
                stalls = 0
                continue

            except Exception as e:
                stalls += 1
                if stalls > MAX_STALLS_PER_SCENE:
                    print(f"[ANALYSIS] {active_model} توقف {stalls} مرات متتالية على نفس هذا المشهد. "
                          f"تسليمه لبقية سلسلة التدرّج (Puter→CLIP→قبول) دون التخلي عن هذا العامل لبقية المشاهد.")
                    return _fallback_cascade(file_path, narration, check_path, is_temp,
                                              skip_puter=(active_model == "puter"))
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


def _lane_worker(lane_name: str) -> None:
    """خيط دائم واحد لكل عامل — يمسك مشهداً تلو الآخر من الطابور المشترك
    طوال عمر العملية، بالتوازي التام مع العامل الآخر."""
    while True:
        job: _VerifyJob = _dispatch_queue.get()
        try:
            job.result = _resolve_job(lane_name, job.file_path, job.narration, job.check_path, job.is_temp)
        except Exception as e:
            print(f"[ANALYSIS] خطأ غير متوقع بعامل {lane_name}: {e}. قبول تلقائي لهذا المشهد لمنع توقف الإنتاج.")
            job.result = True
        finally:
            job.event.set()
            _dispatch_queue.task_done()


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


def verify(file_path: str, narration: str) -> bool:
    """
    الدالة الرئيسية الوحيدة — تُستدعى من أي ملف يحتاج فحص تطابق بصري (نفس
    التوقيع والسلوك الظاهري كما كان بالضبط، فلا حاجة لتعديل أي مستدعٍ).

    الآن تبدأ بفلتر CLIP محلي مجاني (الطبقة 0): لو كان تشابه المشهد مع
    السرد أقل من CLIP_REJECT_THRESHOLD بوضوح، يُرفض المشهد فوراً محلياً
    بلا أي استهلاك لحصة Gemini/Groq اليومية. أي مشهد لا يُرفض هنا — حتى لو
    كان تشابهه مرتفعاً — يُرسَل إلزامياً لطابور Gemini/Groq المشترك تماماً
    كما كان سابقاً؛ هذا الفلتر المسبق لا يملك صلاحية قبول مشهد بمفرده أبداً.

    داخلياً (بعد الفلتر): تضع هذا المشهد كطلب بطابور مشترك يعمل عليه
    عاملان دائمان (Gemini وGroq) بالتوازي طوال التشغيل — كل عامل يمسك
    مشهداً مختلفاً في نفس اللحظة بمجرد أن يصبح متاحاً، بدل ازدواج نفس
    المشهد بينهما. تُحظر (block) حتى يُحسم مشهدها تحديداً ثم ترجع True/False.
    """
    check_path, is_temp = _extract_frame_for_check(file_path)
    if check_path is None:
        print("[ANALYSIS] تعذر استخراج إطار من الفيديو. قبول تلقائي.")
        return True

    # --- الطبقة 0: فلتر CLIP محلي مسبق (رفض فقط، بلا استهلاك حصة) ---
    try:
        similarity = _clip_similarity(check_path, narration)
        print(f"[CLIP L0] فلتر مسبق قبل Gemini/Groq: تشابه {similarity:.3f} "
              f"(عتبة الرفض المبكر: {CLIP_REJECT_THRESHOLD})")
        if similarity < CLIP_REJECT_THRESHOLD:
            print(f"[CLIP L0] رفض مبكر — لن يُستهلك أي حصة Gemini/Groq لهذا المشهد: {narration[:50]}...")
            if is_temp and os.path.exists(check_path):
                try:
                    os.remove(check_path)
                except Exception:
                    pass
            return False
    except Exception as e:
        print(f"[CLIP L0] تعذر تشغيل الفلتر المسبق ({e}). المتابعة مباشرة لتحليل Gemini/Groq الكامل.")

    _ensure_lanes_started()
    job = _VerifyJob(file_path, narration, check_path, is_temp)
    _dispatch_queue.put(job)
    job.event.wait()
    return job.result
