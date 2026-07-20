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
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM DISABLED] {level.upper()}: {message}")
        return

    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(level, "ℹ️")
    text = f"{emoji} *YouTube Automation*\n{message}"

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception as e:
        # لا نفشل الـ pipeline بسبب فشل التنبيه نفسه
        print(f"[TELEGRAM ERROR] فشل إرسال التنبيه: {e}")


def alert_step_failed(step_name: str, error: Exception):
    send_alert(f"فشلت خطوة *{step_name}*:\n`{str(error)[:500]}`", level="error")


def alert_quota_warning(service: str, used: int, limit: int):
    pct = round(used / limit * 100, 1)
    if pct >= 80:
        send_alert(
            f"تحذير حصة: *{service}* استهلك {used}/{limit} ({pct}%)",
            level="warning",
        )
