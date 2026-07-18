"""
retry_utils.py
Exponential backoff ذكي بدل الانتظار الثابت — يحاول فوراً، ولو صدم rate limit
ينتظر تدريجياً (2s -> 4s -> 8s -> 16s) بدل انتظار 30-60 ثانية دائم بلا داعٍ.
"""
import time
import functools
from scripts.telegram_alerts import send_alert


def with_backoff(max_retries: int = 5, base_delay: float = 2.0, exceptions=(Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_error = e
                    is_rate_limit = "429" in str(e) or "quota" in str(e).lower() or "rate" in str(e).lower()
                    if not is_rate_limit and attempt == 0:
                        # خطأ غير متعلق بالحصة، لا داعي للانتظار الطويل، حاول فوراً مرة وحدة إضافية
                        delay = 1
                    else:
                        delay = base_delay * (2 ** attempt)
                    print(f"[RETRY] محاولة {attempt + 1}/{max_retries} فشلت: {e}. الانتظار {delay}s")
                    time.sleep(delay)
            send_alert(f"فشلت {func.__name__} بعد {max_retries} محاولات: {last_error}", level="error")
            raise last_error
        return wrapper
    return decorator
