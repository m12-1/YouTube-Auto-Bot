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
# OAuth client secrets تُستخدم للرفع (videos.insert يحتاج تفويض مستخدم كامل)
# API Key بسيط يكفي لـ search.list / videos.list (قراءة عامة فقط)
# ---------------------------------------------------------------------------
YOUTUBE_OAUTH_CLIENT_ID = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID")
YOUTUBE_OAUTH_CLIENT_SECRET = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET")
YOUTUBE_OAUTH_REFRESH_TOKEN = os.environ.get("YOUTUBE_OAUTH_REFRESH_TOKEN")
YOUTUBE_SEARCH_API_KEY = os.environ.get("YOUTUBE_SEARCH_API_KEY")  # مفتاح منفصل لـ competitor_seo فقط

# ---------------------------------------------------------------------------
# خدمات أخرى
# ---------------------------------------------------------------------------
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # لـ Sheets + Drive
GITHUB_TOKEN = os.environ.get("GH_PAT")  # لإنشاء Pull Requests من self_heal.py

# ---------------------------------------------------------------------------
# إعدادات المحتوى
# ---------------------------------------------------------------------------
CHANNEL_REGION = "US"
CHANNEL_LANGUAGE = "en"

# المرحلة الحالية: شورتس فقط (حسب قرارك). بعد كم أسبوع من نتائج مستقرة،
# فعّل long_pipeline.yml (معطل حالياً بـ workflow_dispatch فقط) وحدّث هذا العلم.
PHASE = "shorts_only"

LONG_VIDEO_TARGET_SECONDS = 5 * 60       # جاهز لمرحلة لاحقة، غير مُفعّل بالجدولة الآن
SHORT_VIDEO_TARGET_SECONDS = 55

# دقة الفيديو الطويل (أفقي)
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
# دقة الشورت (عمودي) — 1080 هو أدنى بُعد مسموح به (العرض)، الارتفاع 1920
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
VIDEO_FPS = 30
MIN_ALLOWED_RESOLUTION = 1080  # الحد الأدنى المطلق لأي بُعد (عرض أو ارتفاع)

# هوامش الأمان لواجهة يوتيوب شورتس (Safe Zone) — بالنسبة المئوية من الأبعاد
SHORTS_SAFE_ZONE = {
    "top_percent": 8,       # شريط علوي (قد يظهر فيه اسم القناة بمعاينات معينة)
    "right_percent": 14,    # أزرار like/comment/share/subscribe الجانبية
    "bottom_percent": 26,   # منطقة العنوان/الوصف/الصوت بالأسفل
    "left_percent": 4,      # مسافة جانبية بسيطة كما طلبت (توازن بصري)
}
DEDUP_LOOKBACK_DAYS = 60
QUALITY_GATE_MIN_SCORE = 7  # من 10 — لو أقل، يعيد المحاولة

# فئات YouTube المحظورة من الترشيح كترند (أخبار/سياسة تحديداً)
BLOCKED_CATEGORY_IDS = {"25", "29"}  # News & Politics, Nonprofits & Activism
# قائمة الكلمات المحظورة الكاملة (خمر/مخدرات/عري/قمار/جنس + حساسية إخبارية)
# انتقلت لملف مستقل content_policy.py لأنها تُستخدم بـ 3 نقاط مختلفة بالخط
# (trend_scanner + script_writer + quality_gate)، راجعه لتعديل/إضافة كلمات.

# ---------------------------------------------------------------------------
# نماذج Gemini المستخدمة لكل مهمة (سهل التحديث من مكان واحد)
# ---------------------------------------------------------------------------
MODEL_TREND_FILTER = "gemini-3.1-flash-lite"
MODEL_QUALITY_GATE = "gemini-2.5-flash"
MODEL_SCRIPT_WRITER = "gemini-2.5-pro"
MODEL_SEO = "gemini-2.5-pro"
MODEL_THUMBNAIL = "gemini-3-pro-image"
MODEL_EMBEDDING = "gemini-embedding-2"


@dataclass
class Paths:
    sheets_current_plan: str = "Current_Plan"
    sheets_daily_log: str = "Daily_Log"
    sheets_trend_log: str = "Trend_Log"
    sheets_system_control: str = "System_Control"


def require(*names):
    """يتأكد إن كل الأسرار المطلوبة لمهمة معينة موجودة، ويفشل بوضوح إذا ناقصة."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise EnvironmentError(
            f"الأسرار التالية ناقصة من GitHub Secrets: {', '.join(missing)}. "
            f"راجع SECRETS.md."
        )
