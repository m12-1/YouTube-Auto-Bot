"""
نقطة تشغيل الإنتاج المرتبطة بالخطة (نقطة 3 من طلب المستخدم).

لا يغيّر أي شيء في مراحل الرندر/التحقق من المشاهد/التدقيق - فقط يستبدل مصدر
"الموضوع" (topic) من اختيار جمناي المباشر (topic_selector) إلى صف مستحق
النشر من جدول Plan في Google Sheets، ثم يستدعي نفس run_pipeline() الموجود
في main.py دون أي تعديل على منطقها الداخلي.

بعد نجاح النشر: يُحدَّث صف Plan إلى status=published، ويُضاف صف جديد في
جدول Stats يحمل بيانات الفيديو (الفئة، الموضوع، العنوان، السكربت، وقت
النشر) + مواعيد استحقاق فحص الإحصائيات بعد 24/48/72 ساعة (تُملأ لاحقًا
عبر planner/stats_updater.py).

يُشغَّل بشكل متكرر (كل 15 دقيقة تقريبًا) عبر
.github/workflows/production_from_plan.yml، وفي كل تشغيل يلتقط صفًا واحدًا
فقط (أقرب موعد مستحق) لتفادي رندر عدة فيديوهات متزامنة.
"""

import logging
from datetime import datetime, timedelta, timezone

from planner import sheets_client
from planner.config_planner import TAB_PLAN, TAB_STATS
from main import run_pipeline

logger = logging.getLogger("run_from_plan")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ترتيب أعمدة Plan: row_id, category, topic, scheduled_date, scheduled_time_et,
# scheduled_datetime_utc, status, video_id, notes  -> status يبدأ من العمود G
_PLAN_STATUS_START_COL = "G"

_STATS_HEADER_ORDER = [
    "video_id", "category", "topic", "title", "script", "published_at_utc",
    "check_24h_due_utc", "views_24h", "likes_24h", "comments_24h",
    "check_48h_due_utc", "views_48h", "likes_48h", "comments_48h",
    "check_72h_due_utc", "views_72h", "likes_72h", "comments_72h", "stats_complete",
    "avg_view_percentage_72h", "avg_view_duration_sec_72h",
    "slot_bucket", "is_exploration", "angle",
]


def _next_due_row():
    rows = sheets_client.read_all(TAB_PLAN)
    now = datetime.now(timezone.utc)
    due = []
    for r in rows:
        if r.get("status") != "pending":
            continue
        try:
            dt = datetime.fromisoformat(r["scheduled_datetime_utc"])
        except (KeyError, ValueError):
            continue
        if dt <= now:
            due.append((dt, r))
    if not due:
        return None
    due.sort(key=lambda x: x[0])
    return due[0][1]


def main():
    row = _next_due_row()
    if row is None:
        logger.info("لا يوجد أي صف مستحق النشر الآن في جدول Plan.")
        return

    row_number = int(row["_row_number"])
    topic = row["topic"]
    category = row["category"]

    # قفل فوري لمنع أي تشغيل متزامن آخر (أو تشغيل تالٍ خلال نفس الـ15 دقيقة) من
    # التقاط نفس الصف مرتين قبل انتهاء هذا الرندر (الذي قد يستغرق دقائق طويلة)
    sheets_client.update_row(TAB_PLAN, row_number, ["in_progress", "", ""], start_col=_PLAN_STATUS_START_COL)
    logger.info("بدء إنتاج الفيديو -> الفئة: %s | الموضوع: %s", category, topic)

    try:
        result = run_pipeline(
            category=category, dry_run_publish=False, privacy_status="public", fixed_topic=topic,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("فشل تنفيذ خط الإنتاج للصف %s: %s", row["row_id"], e)
        sheets_client.update_row(
            TAB_PLAN, row_number, ["failed", "", str(e)[:300]], start_col=_PLAN_STATUS_START_COL
        )
        return

    video_id = result.get("video_id", "")
    seo = result.get("seo", {})
    if result.get("manual_review_required"):
        note = result.get("message", "يحتاج مراجعة يدوية قبل النشر العام.")[:300]
        sheets_client.update_row(TAB_PLAN, row_number, ["needs_review", video_id, note], start_col=_PLAN_STATUS_START_COL)
        logger.warning("الصف %s يحتاج مراجعة يدوية (video_id=%s)، حالة الشيت: needs_review.", row["row_id"], video_id)
    else:
        sheets_client.update_row(TAB_PLAN, row_number, ["published", video_id, ""], start_col=_PLAN_STATUS_START_COL)

    now_utc = datetime.now(timezone.utc)
    stats_row = {h: "" for h in _STATS_HEADER_ORDER}
    stats_row.update({
        "video_id": video_id,
        "category": category,
        "topic": topic,
        "title": seo.get("optimized_title", ""),
        "script": result.get("narration", ""),
        "published_at_utc": now_utc.isoformat(),
        "check_24h_due_utc": (now_utc + timedelta(hours=24)).isoformat(),
        "check_48h_due_utc": (now_utc + timedelta(hours=48)).isoformat(),
        "check_72h_due_utc": (now_utc + timedelta(hours=72)).isoformat(),
        "stats_complete": "FALSE",
        "avg_view_percentage_72h": "",
        "avg_view_duration_sec_72h": "",
        "slot_bucket": row.get("slot_bucket", ""),
        "is_exploration": row.get("is_exploration", "FALSE"),
        "angle": row.get("angle", ""),
    })
    sheets_client.append_rows(TAB_STATS, [[stats_row[h] for h in _STATS_HEADER_ORDER]])
    logger.info("تم النشر وتسجيل صف الإحصائيات بنجاح: video_id=%s", video_id)


if __name__ == "__main__":
    main()
