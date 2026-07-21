"""
telegram_alerts.py
إرسال تنبيهات فورية لتليجرام عند أي خطأ أو حدث مهم.
"""
import requests
from scripts import config


def send_alert(message: str, level: str = "info"):
    """
    level: info | warning | error
    """
    # نطبع دائماً بسجل التشغيل أولاً (بغض النظر عن نجاح/فشل الإرسال أو تفعيل
    # تليجرام) — سابقاً كانت الرسائل تصل لتليجرام فقط ولا تظهر أبداً بالـ log،
    # ما يصعّب تتبع الأخطاء لاحقاً من سجل التشغيل وحده.
    print(f"[TELEGRAM ALERT] {level.upper()}: {message}")

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[TELEGRAM DISABLED] لم يُرسل فعلياً — TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين")
        return

    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(level, "ℹ️")
    text = f"{emoji} *YouTube Automation*\n{message}"

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        # لا نفشل الـ pipeline بسبب فشل التنبيه نفسه، لكن نسجّل الفشل بوضوح
        print(f"[TELEGRAM ERROR] فشل إرسال التنبيه فعلياً لتليجرام: {e}")


def alert_step_failed(step_name: str, error: Exception):
    send_alert(f"فشلت خطوة *{step_name}*:\n`{str(error)[:500]}`", level="error")


def alert_quota_warning(service: str, used: int, limit: int):
    pct = round(used / limit * 100, 1)
    if pct >= 80:
        send_alert(
            f"تحذير حصة: *{service}* استهلك {used}/{limit} ({pct}%)",
            level="warning",
        )


def alert_key_error(service: str, key_name: str, error: str):
    """تنبيه فوري عند توقف مفتاح API عن العمل (ليس نفاد حصة بل عطل فعلي).
    لا تُستدعى عند أخطاء الحصة (429) — فقط عند أخطاء أخرى مثل مفتاح منتهي
    الصلاحية أو محذوف أو خطأ مصادقة."""
    send_alert(
        f"🔑 *مفتاح {service} معطّل!*\n"
        f"المفتاح: `{key_name}`\n"
        f"الخطأ: `{str(error)[:400]}`\n"
        f"⚠️ هذا ليس نفاد حصة — المفتاح نفسه لا يعمل. يرجى التحقق منه.",
        level="error",
    )
