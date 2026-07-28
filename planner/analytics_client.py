"""
عميل YouTube Analytics API - يجلب نسبة الاحتفاظ الحقيقية بالمشاهد (Audience
Retention)، أهم مقياس مفقود في التصميم الأصلي (views/likes/comments مقاييس
سطحية جدًا مقارنة بها).

=== خطوة إعداد لمرة واحدة مطلوبة منك ===
النشر الحالي (modules/publisher.py) يستخدم refresh_token بصلاحية واحدة فقط:
    https://www.googleapis.com/auth/youtube.upload
هذه الصلاحية لا تكفي لقراءة الـ Analytics. تحتاج صلاحية إضافية:
    https://www.googleapis.com/auth/yt-analytics.readonly

الخطوات:
1. استخدم نفس OAuth Client الحالي (نفس YOUTUBE_OAUTH_CLIENT_ID/SECRET - لا حاجة
   لعميل جديد في Google Cloud Console).
2. أعد عمل موافقة OAuth مرة واحدة (OAuth consent / "Get refresh token" flow)
   لكن اطلب هذه المرة الصلاحيتين معًا في نفس الطلب:
   - https://www.googleapis.com/auth/youtube.upload
   - https://www.googleapis.com/auth/yt-analytics.readonly
   ستحصل على refresh_token واحد جديد يحمل الصلاحيتين معًا.
3. أضف هذا الـ refresh_token الجديد في GitHub Secrets تحت اسم متغيّر البيئة:
        YOUTUBE_ANALYTICS_REFRESH_TOKEN
   (يمكنك أيضًا استبدال القيمة القديمة لـ YOUTUBE_OAUTH_REFRESH_TOKEN بنفس
   القيمة الجديدة طالما تحمل الصلاحيتين معًا - لكن الأسلم إبقاءهما منفصلين
   حتى لا يتعطّل النشر لو حدث خطأ في إعداد الـ Analytics).

إن لم يكن هذا السر موجودًا في البيئة، كل الاستدعاءات هنا تعيد None بهدوء
والنظام يستمر بالعمل بمقاييس views/likes/comments فقط دون أي كسر.
"""

import os
import logging
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger("planner.analytics_client")

_REFRESH_TOKEN_ENV = "YOUTUBE_ANALYTICS_REFRESH_TOKEN"
_REQUIRED_ENV = ["YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET", _REFRESH_TOKEN_ENV]

_client_cache = None


def is_configured() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED_ENV)


def _get_client():
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    creds = Credentials(
        token=None,
        refresh_token=os.environ[_REFRESH_TOKEN_ENV],
        client_id=os.environ["YOUTUBE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=None,
    )
    creds.refresh(Request())
    _client_cache = build("youtubeAnalytics", "v2", credentials=creds)
    return _client_cache


def get_retention(video_id: str, published_at_utc: datetime) -> dict:
    """يعيد {"avg_view_percentage": float, "avg_view_duration_sec": float}
    أو None إن لم تتوفر بيانات كافية بعد أو لم يُفعَّل السر أو حدث أي خطأ
    (يُسجَّل تحذيرًا فقط، لا يوقف stats_updater.py أبدًا)."""
    if not is_configured():
        return None
    try:
        client = _get_client()
        start_date = published_at_utc.date().isoformat()
        end_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        resp = client.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="averageViewPercentage,averageViewDuration",
            filters=f"video=={video_id}",
        ).execute()
        rows = resp.get("rows") or []
        if not rows:
            logger.info("لا توجد بيانات retention بعد للفيديو %s (طبيعي خلال أول ساعات النشر).", video_id)
            return None
        avg_pct, avg_dur = rows[0][0], rows[0][1]
        return {"avg_view_percentage": float(avg_pct), "avg_view_duration_sec": float(avg_dur)}
    except Exception as e:  # noqa: BLE001
        logger.warning("تعذّر جلب retention للفيديو %s: %s", video_id, e)
        return None
