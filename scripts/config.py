"""
config.py
الإعدادات المركزية لكل النظام + منطق توزيع المفاتيح.
كل القيم الحساسة تُقرأ من GitHub Secrets (متغيرات بيئة)، لا يوجد أي مفتاح مكتوب هنا.
"""
import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# مفاتيح Gemini الثلاثة (راجع SECRETS.md لشرح كل واحد)
# ---------------------------------------------------------------------------
GEMINI_KEY_LIGHT = os.environ.get("GEMINI_KEY_LIGHT")     # فحص ترندات + Quality Gate
GEMINI_KEY_ADVANCED = os.environ.get("GEMINI_KEY_ADVANCED")  # كتابة سكربت + SEO
GEMINI_KEY_IMAGE = os.environ.get("GEMINI_KEY_IMAGE")     # توليد الصور المصغّرة فقط

# ---------------------------------------------------------------------------
# مفاتيح YouTube (مفتاحين منفصلين حسب الاتفاق: رفع / بحث منافسين)
# ---------------------------------------------------------------------------
YOUTUBE_OAUTH_CLIENT_ID = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID")
YOUTUBE_OAUTH_CLIENT_SECRET = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET")
YOUTUBE_OAUTH_REFRESH_TOKEN = os.environ.get("YOUTUBE_OAUTH_REFRESH_TOKEN")
YOUTUBE_SEARCH_API_KEY = os.environ.get("YOUTUBE_SEARCH_API_KEY")

# ---------------------------------------------------------------------------
# خدمات أخرى
# ---------------------------------------------------------------------------
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GITHUB_TOKEN = os.environ.get("GH_PAT")

# ---------------------------------------------------------------------------
# إعدادات المحتوى
# ---------------------------------------------------------------------------
CHANNEL_REGION = "US"
CHANNEL_LANGUAGE = "en"
PHASE = "shorts_only"

LONG_VIDEO_TARGET_SECONDS = 5 * 60
SHORT_VIDEO_TARGET_SECONDS = 55

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
VIDEO_FPS = 30
MIN_ALLOWED_RESOLUTION = 1080

SHORTS_SAFE_ZONE = {
    "top_percent": 8,
    "right_percent": 14,
    "bottom_percent": 26,
    "left_percent": 4,
}
DEDUP_LOOKBACK_DAYS = 60
QUALITY_GATE_MIN_SCORE = 7

# وضع الاختبار: لو True، أي فيديو يُنشر يكون "private" بدل "public" — يُفعّل
# تلقائياً من واجهة GitHub Actions (workflow_dispatch) عند التشغيل اليدوي
TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() == "true"

BLOCKED_CATEGORY_IDS = {"25", "29"}

# ---------------------------------------------------------------------------
# نماذج Gemini المحدثة لعام 2026 (مستقرة ومتاحة)
# ---------------------------------------------------------------------------
MODEL_TREND_FILTER = "gemini-3.1-flash-lite"
MODEL_QUALITY_GATE = "gemini-3.1-flash-lite"
MODEL_SCRIPT_WRITER = "gemini-3.5-flash"
MODEL_SEO = "gemini-3.5-flash"
MODEL_THUMBNAIL = "gemini-3.1-flash-lite-image"
MODEL_EMBEDDING = "gemini-embedding-2"


@dataclass
class Paths:
    sheets_current_plan: str = "Current_Plan"
    sheets_daily_log: str = "Daily_Log"
    sheets_trend_log: str = "Trend_Log"
    sheets_system_control: str = "System_Control"


def require(*names):
    """يتأكد إن كل الأسرار المطلوبة لمهمة معينة موجودة."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise EnvironmentError(
            f"الأسرار التالية ناقصة من GitHub Secrets: {', '.join(missing)}. "
            f"راجع SECRETS.md."
        )
