"""
telegram_alerts.py
إرسال تنبيهات فورية لتليجرام عند أي خطأ أو حدث مهم.
"""
import threading
import requests
from scripts import config

# --- منع تكرار نفس التنبيه ---
# كل (service, error_type) يُرسل مرة واحدة فقط لكل تشغيل — التكرارات
# تُسجّل بالـ log فقط بلا إرسال فعلي لتليجرام، حتى لا يُغرَق المستخدم
# بعشرات الرسائل المتطابقة (مثل "Puter معطّل" × عدد المشاهد).
_sent_alert_keys_lock = threading.Lock()
_sent_alert_keys: set = set()


def _is_duplicate_alert(dedup_key: str) -> bool:
    """يتحقق إذا سبق إرسال تنبيه بنفس المفتاح بهذا التشغيل. يسجّل المفتاح
    لو لم يُسجَّل مسبقاً ويرجع False (أول مرة)، أو يرجع True (مكرر)."""
    with _sent_alert_keys_lock:
        if dedup_key in _sent_alert_keys:
            return True
        _sent_alert_keys.add(dedup_key)
        return False


def send_alert(message: str, level: str = "info", dedup_key: str = None):
    """
    level: info | warning | error
    """
    # نطبع دائماً بسجل التشغيل أولاً (بغض النظر عن نجاح/فشل الإرسال أو تفعيل
    # تليجرام) — سابقاً كانت الرسائل تصل لتليجرام فقط ولا تظهر أبداً بالـ log،
    # ما يصعّب تتبع الأخطاء لاحقاً من سجل التشغيل وحده.
    print(f"[TELEGRAM ALERT] {level.upper()}: {message}")

    # منع تكرار نفس التنبيه لتليجرام لو مُرِّر dedup_key
    if dedup_key and _is_duplicate_alert(dedup_key):
        print(f"[TELEGRAM DEDUP] تم تخطي إرسال تنبيه مكرر (المفتاح: {dedup_key}) — سبق إرساله.")
        return

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[TELEGRAM DISABLED] لم يُرسل فعلياً — TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين")
        return

    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(level, "ℹ️")
    text = f"{emoji} YouTube Automation\n{message}"

    # ملاحظة إصلاح (400 Bad Request المتكرر بالسجل):
    # كنا نلف الرسالة بـ parse_mode="Markdown" مع محتوى ديناميكي (أسماء دوال
    # فيها "_" مثل _generate_text_internal، ونصوص أخطاء JSON خام فيها *و_
    # وأقواس). Markdown القديم بتليجرام يفشل فوراً (400: "can't find end of
    # the entity") إذا كان عدد أي رمز تنسيق (* _ `) غير متزازن، وهذا يحدث
    # حتماً مع نص عشوائي كهذا لا نتحكم بمحتواه. الحل: إرسال كنص عادي بدون
    # parse_mode أصلاً — الأهم هو وصول التنبيه فعلياً لا تنسيقه.
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        # لا نفشل الـ pipeline بسبب فشل التنبيه نفسه، لكن نسجّل الفشل بوضوح
        print(f"[TELEGRAM ERROR] فشل إرسال التنبيه فعلياً لتليجرام: {e}")


def alert_step_failed(step_name: str, error: Exception):
    send_alert(f"فشلت خطوة {step_name}:\n{str(error)[:500]}", level="error")


def alert_quota_warning(service: str, used: int, limit: int):
    pct = round(used / limit * 100, 1)
    if pct >= 80:
        send_alert(
            f"تحذير حصة: {service} استهلك {used}/{limit} ({pct}%)",
            level="warning",
        )


def alert_key_error(service: str, key_name: str, error: str):
    """تنبيه فوري عند توقف مفتاح API عن العمل (ليس نفاد حصة بل عطل فعلي).
    لا تُستدعى عند أخطاء الحصة (429) — فقط عند أخطاء أخرى مثل مفتاح منتهي
    الصلاحية أو محذوف أو خطأ مصادقة.
    
    يُرسل تنبيه واحد فقط لكل (service) بهذا التشغيل — التكرارات تُسجّل
    بالـ log فقط بلا إرسال فعلي لتليجرام."""
    send_alert(
        f"🔑 مفتاح {service} معطّل!\n"
        f"المفتاح: {key_name}\n"
        f"الخطأ: {str(error)[:400]}\n"
        f"⚠️ هذا ليس نفاد حصة — المفتاح نفسه لا يعمل. يرجى التحقق منه.",
        level="error",
        dedup_key=f"key_error:{service}",
    )
