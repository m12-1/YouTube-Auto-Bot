"""
محدّث الإحصائيات (نقطة 4 من طلب المستخدم الأصلي + نقطة 5 من طلب التحسين).

يفحص دوريًا جدول Stats، ولأي فيديو استحق موعد 24/48/72 ساعة منذ نشره ولم
تُسجَّل بياناته بعد، يجلب views/likes/comments عبر YouTube Data API
(باستخدام YOUTUBE_SEARCH_API_KEY العام - بيانات إحصائية عامة لا تحتاج OAuth
ولا صلاحية رفع)، ويكتبها في الأعمدة المخصصة.

عند نقطة الـ72 ساعة تحديدًا، يحاول أيضًا جلب نسبة الاحتفاظ الحقيقية بالمشاهد
(Audience Retention) عبر planner/analytics_client.py - المقياس الحقيقي لجودة
السكربت/الهوك الذي كان مفقودًا تمامًا في التصميم السابق. إن لم يكن سر
YOUTUBE_ANALYTICS_REFRESH_TOKEN مفعّلًا بعد، يتجاهل هذه الخطوة بهدوء دون كسر
أي شيء - النظام يستمر بالعمل بمقاييس views/likes/comments فقط.

مستقل تمامًا عن خط إنتاج الفيديو ويعمل بجدول زمني خاص به
(.github/workflows/stats_updater.yml).
"""

import os
import logging
from datetime import datetime, timezone

from googleapiclient.discovery import build

from planner import sheets_client, analytics_client
from planner.config_planner import TAB_STATS

logger = logging.getLogger("planner.stats_updater")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_HEADER_ORDER = [
    "video_id", "category", "topic", "title", "script", "published_at_utc",
    "check_24h_due_utc", "views_24h", "likes_24h", "comments_24h",
    "check_48h_due_utc", "views_48h", "likes_48h", "comments_48h",
    "check_72h_due_utc", "views_72h", "likes_72h", "comments_72h", "stats_complete",
    "avg_view_percentage_72h", "avg_view_duration_sec_72h",
    "slot_bucket", "is_exploration", "angle",
]

_CHECKPOINTS = {
    "24h": {"due": "check_24h_due_utc", "views": "views_24h", "likes": "likes_24h", "comments": "comments_24h"},
    "48h": {"due": "check_48h_due_utc", "views": "views_48h", "likes": "likes_48h", "comments": "comments_48h"},
    "72h": {"due": "check_72h_due_utc", "views": "views_72h", "likes": "likes_72h", "comments": "comments_72h"},
}


def _youtube_public_client():
    key = os.environ.get("YOUTUBE_SEARCH_API_KEY")
    if not key:
        raise EnvironmentError("YOUTUBE_SEARCH_API_KEY غير موجود في البيئة.")
    return build("youtube", "v3", developerKey=key)


def _fetch_stats(youtube, video_id: str) -> dict:
    resp = youtube.videos().list(part="statistics", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        return {}
    stats = items[0].get("statistics", {})
    return {
        "views": stats.get("viewCount", "0"),
        "likes": stats.get("likeCount", "0"),
        "comments": stats.get("commentCount", "0"),
    }


def _maybe_fetch_retention(r: dict) -> bool:
    """يُستدعى فقط عند اكتمال نقطة الـ72 ساعة. يعيد True إن تمت إضافة بيانات جديدة."""
    if r.get("avg_view_percentage_72h"):
        return False  # مسجّلة مسبقًا
    if not analytics_client.is_configured():
        return False
    video_id = r.get("video_id")
    published_at = r.get("published_at_utc")
    if not video_id or not published_at:
        return False
    try:
        published_dt = datetime.fromisoformat(published_at)
    except ValueError:
        return False

    data = analytics_client.get_retention(video_id, published_dt)
    if not data:
        return False
    r["avg_view_percentage_72h"] = data["avg_view_percentage"]
    r["avg_view_duration_sec_72h"] = data["avg_view_duration_sec"]
    logger.info("سُجّلت retention للفيديو %s: %.1f%% متوسط نسبة المشاهدة", video_id, data["avg_view_percentage"])
    return True


def main():
    rows = sheets_client.read_all(TAB_STATS)
    if not rows:
        logger.info("جدول Stats فارغ حاليًا.")
        return

    youtube = _youtube_public_client()
    now = datetime.now(timezone.utc)
    updated_count = 0

    for r in rows:
        if str(r.get("stats_complete")).strip().upper() == "TRUE":
            continue
        video_id = r.get("video_id")
        if not video_id:
            continue

        changed = False
        for checkpoint_name, cols in _CHECKPOINTS.items():
            if r.get(cols["views"]):  # هذه النقطة مسجّلة مسبقًا
                continue
            try:
                due = datetime.fromisoformat(r[cols["due"]])
            except (KeyError, ValueError):
                continue
            if now < due:
                continue  # لم يحن الموعد بعد

            data = _fetch_stats(youtube, video_id)
            if not data:
                logger.warning("تعذّر جلب إحصائيات الفيديو %s (قد يكون محذوفًا/خاصًا).", video_id)
                continue

            r[cols["views"]] = data["views"]
            r[cols["likes"]] = data["likes"]
            r[cols["comments"]] = data["comments"]
            changed = True
            logger.info("سُجّلت %s للفيديو %s: %s مشاهدة", cols["views"], video_id, data["views"])

            if checkpoint_name == "72h" and _maybe_fetch_retention(r):
                changed = True

        # لو نافذة الـ72 ساعة مكتملة (views_72h موجودة من تشغيل سابق) لكن
        # retention لم يُجلب بعد، نحاول جلبها مجددًا هنا حتى لو لم يتغيّر أي
        # checkpoint في هذا التشغيل تحديدًا - بيانات Analytics غالبًا تتأخّر
        # معالجتها 24-48 ساعة إضافية عن due_72h.
        if r.get("views_72h") and not r.get("avg_view_percentage_72h"):
            if _maybe_fetch_retention(r):
                changed = True

        if r.get("views_72h") and str(r.get("stats_complete")).strip().upper() != "TRUE":
            retention_pending = analytics_client.is_configured() and not r.get("avg_view_percentage_72h")
            within_retry_window = True
            try:
                due_72h = datetime.fromisoformat(r["check_72h_due_utc"])
                within_retry_window = (now - due_72h).days < 5  # نافذة إعادة محاولة لـ retention المتأخرة
            except (KeyError, ValueError):
                pass
            if not (retention_pending and within_retry_window):
                r["stats_complete"] = "TRUE"
                changed = True

        if changed:
            values = [r.get(h, "") for h in _HEADER_ORDER]
            sheets_client.update_row(TAB_STATS, int(r["_row_number"]), values)
            updated_count += 1

    logger.info("انتهى فحص الإحصائيات - عدد الصفوف المحدَّثة: %d", updated_count)


if __name__ == "__main__":
    main()
