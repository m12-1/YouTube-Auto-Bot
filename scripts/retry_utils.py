"""
retry_utils.py
Exponential backoff ذكي مع احترام retry-after من الخادم — لو الخادم يقول
"انتظر 59 ثانية" ننتظرها بدل 3-6 ثوانٍ عشوائية اللي تضيّع كل المحاولات بلا فائدة.
"""
import re
import time
import threading
import functools
from scripts.telegram_alerts import send_alert


class RateLimiter:
    """يضمن ألا يقل الفاصل الزمني بين طلبين متتاليين لنفس الطبقة (Gemini أو
    Groq) عن `min_interval` ثانية، حتى لو استُدعيت الدالة من عدة خيوط
    (threads) بنفس الوقت — لأن الآن التحليل يعمل بالتوازي (Gemini + Groq
    بنفس الوقت لعناصر مختلفة) وقد يعمل بالتوازي أيضاً على أكثر من عنصر
    بنفس الطبقة، فبدون هذا القفل يمكن أن نُنفّذ حصة الـ API المجانية
    بسرعة أكبر بكثير مما هو مسموح.
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                sleep_for = self.min_interval - elapsed
                time.sleep(sleep_for)
            self._last_call = time.monotonic()


def _extract_retry_delay(error_msg: str) -> float:
    """يستخرج مدة الانتظار الموصى بها من رسالة خطأ 429 — مثلاً:
    'Please retry in 59.473636795s' → 59.47"""
    match = re.search(r'retry in (\d+\.?\d*)s', error_msg, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 2, 120)  # +2s هامش أمان، أقصى 120s
    return 0


# أخطاء برمجية بحتة (Programming Errors) لن تُحل أبداً بالانتظار وإعادة
# المحاولة — إعادة محاولتها مجرد هدر وقت مضمون. مثلاً: AttributeError من
# client.api_key يفشل بنفس الطريقة تماماً 6 مرات متتالية × backoff.
_FATAL_ERROR_TYPES = (
    AttributeError,
    TypeError,
    ValueError,
    ImportError,
    NameError,
    SyntaxError,
)


def with_backoff(max_retries: int = 6, base_delay: float = 3.0, exceptions=(Exception,), fail_fast_predicate=None,
                  alert_level: str = "warning"):
    """
    fail_fast_predicate: دالة اختيارية (error_str) -> bool. لو رجعت True،
    نتوقف فوراً بلا أي محاولة إضافية وبلا انتظار — تُستخدم لأخطاء مضمون
    عدم تعافيها خلال نافذة إعادة المحاولة (مثل استنفاد حصة يومية)، لتفادي
    هدر دقائق كاملة بانتظار خادم لن يستجيب قبل ساعات.

    alert_level: مستوى تنبيه Telegram المُرسَل عند استنفاد كل المحاولات هنا
    (افتراضياً "warning" وليس "error"). السبب: with_backoff يُطبَّق على
    دوال داخلية جداً (مثل _generate_text_internal) قد يملك المستدعي
    الخارجي (مثل generate_text) منطق تعافٍ إضافي بعدها (موديل أخف، مفتاح
    جوكر). إرسال "error" هنا مباشرة يعطي انطباعاً بفشل نهائي كارثي بينما
    النظام قد يتعافى بالسطر التالي فوراً. مرّر alert_level="error" فقط لو
    كانت هذه فعلاً آخر محطة تعافٍ ممكنة بلا أي بديل بعدها.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_error = e
                    error_str = str(e)

                    # أخطاء برمجية بحتة — لن تتعافى أبداً بالانتظار.
                    # نرفعها فوراً بلا أي محاولة إضافية ولا تنبيه (لأن
                    # المستدعي سيتولى التعامل معها بطريقة أنسب).
                    if isinstance(e, _FATAL_ERROR_TYPES):
                        print(f"[RETRY] {func.__name__}: خطأ برمجي ({type(e).__name__}) لن يتعافى بالانتظار. "
                              f"رفع فوري بلا محاولات إضافية: {e}")
                        raise

                    # أخطاء "unexpected keyword argument" هي أيضاً أخطاء
                    # توقيع دالة — برمجية بحتة، لا علاقة لها بالشبكة.
                    if "unexpected keyword argument" in error_str or "got multiple values for argument" in error_str:
                        print(f"[RETRY] {func.__name__}: خطأ توقيع دالة لن يتعافى بالانتظار. "
                              f"رفع فوري بلا محاولات إضافية: {e}")
                        raise

                    if fail_fast_predicate is not None and fail_fast_predicate(error_str):
                        print(f"[RETRY] {func.__name__}: خطأ لن يتعافى خلال نافذة الانتظار (على الأرجح حصة يومية). "
                              f"التوقف فوراً بلا محاولات إضافية.")
                        break

                    is_rate_limit = "429" in error_str or "quota" in error_str.lower() or "rate" in error_str.lower()

                    if is_rate_limit:
                        # نحترم مدة الانتظار اللي يقولها الخادم فعلياً
                        server_delay = _extract_retry_delay(error_str)
                        delay = max(server_delay, base_delay * (2 ** attempt))
                    elif attempt == 0:
                        # خطأ غير متعلق بالحصة — نحاول فوراً مرة واحدة إضافية
                        delay = 1
                    else:
                        delay = base_delay * (2 ** attempt)

                    print(f"[RETRY] محاولة {attempt + 1}/{max_retries} فشلت: {e}. الانتظار {delay:.0f}s")
                    time.sleep(delay)
            send_alert(f"فشلت {func.__name__} بعد {max_retries} محاولات: {last_error}", level=alert_level)
            raise last_error
        return wrapper
    return decorator

