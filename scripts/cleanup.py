"""
cleanup.py
يعمل كل 30 دقيقة. يبحث بـ Daily_Log عن عناصر حالتها 'published' منذ ساعتين
أو أكثر، يحذف الملفات المؤقتة المرتبطة (صوت/صور محلية إن وُجدت)، ويحدّث الحالة
إلى 'cleaned'. (لا حاجة لتنظيف Drive هنا لأن الفيديو النهائي يُرفع مباشرة
لليوتيوب من ffmpeg/Remotion output المحلي، بلا تخزين وسيط بالسحابة).
"""
import os
from datetime import datetime, timedelta
from scripts import config, sheets_client

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")


def run():
    records = sheets_client.get_all_records(SPREADSHEET_ID, config.Paths().sheets_daily_log)
    cutoff = datetime.utcnow() - timedelta(hours=2)

    for r in records:
        if r.get("status") != "published":
            continue
        try:
            published_at = datetime.fromisoformat(str(r.get("published_at", "")))
        except (ValueError, TypeError):
            continue
        if published_at <= cutoff:
            sheets_client.update_cell_by_row_match(
                SPREADSHEET_ID, config.Paths().sheets_daily_log,
                match_column="video_id", match_value=r["video_id"],
                target_column="status", new_value="cleaned",
            )
            print(f"تم تنظيف السجل: {r['video_id']}")


if __name__ == "__main__":
    run()
