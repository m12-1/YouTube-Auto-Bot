"""
retry_utils.py
Exponential backoff ذكي مع احترام retry-after من الخادم — لو الخادم يقول
"انتظر 59 ثانية" ننتظرها بدل 3-6 ثوانٍ عشوائية اللي تضيّع كل المحاولات بلا فائدة.
"""
import re
import time
import functools
from scripts.telegram_alerts import send_alert


def _extract_retry_delay(error_msg: str) -> float:
    """يستخرج مدة الانتظار الموصى بها من رسالة خطأ 429 — مثلاً:
    'Please retry in 59.473636795s' → 59.47"""
    match = re.search(r'retry in (\d+\.?\d*)s', error_msg, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 2, 120)  # +2s هامش أمان، أقصى 120s
    return 0


def with_backoff(max_retries: int = 6, base_delay: float = 3.0, exceptions=(Exception,)):
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
            send_alert(f"فشلت {func.__name__} بعد {max_retries} محاولات: {last_error}", level="error")
            raise last_error
        return wrapper
    return decorator

