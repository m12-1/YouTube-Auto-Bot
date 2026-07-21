"""
daily_pipeline.py
المنسق الخاص بمسار الفيديو الطويل (5 دقائق) — معطّل حالياً بالجدولة
(long_video_pipeline_FUTURE.yml) لحين استقرار مسار الشورتس. نفس فكرة
shorts_pipeline.py: المونتاج الفعلي انتقل بالكامل إلى
scripts/video_montage.py، وهذا الملف يبقى فقط تسلسل الخطوات العام.
"""
import os
from scripts import config, sheets_client, script_writer, quality_gate
from scripts import thumbnail_generator, seo_optimizer, publish, video_montage
from scripts.telegram_alerts import send_alert, alert_step_failed

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKDIR = "pipeline_output"


def run():
    if not sheets_client.is_system_enabled(SPREADSHEET_ID):
        print("النظام متوقف عبر System_Control. تخطي.")
        return

    os.makedirs(WORKDIR, exist_ok=True)

    try:
        trend_records = sheets_client.get_all_records(SPREADSHEET_ID, config.Paths().sheets_trend_log)
        if not trend_records:
            send_alert("لا يوجد موضوع بـ Trend_Log لبدء الإنتاج اليوم.", level="warning")
            return
        topic = trend_records[-1]["core_topic"]

        long_script = script_writer.write_long_script(topic)
        narration_text = script_writer.full_narration_text(long_script)
        evaluation = quality_gate.evaluate(narration_text)
        if not evaluation["passed"]:
            long_script = script_writer.write_long_script(topic)
            narration_text = script_writer.full_narration_text(long_script)
            evaluation = quality_gate.evaluate(narration_text)
            if not evaluation["passed"]:
                send_alert("توقف الإنتاج اليوم: السكربت رسب بـ Quality Gate مرتين.", level="error")
                return

        montage_result = video_montage.build_long_montage(
            long_script=long_script,
            narration_text=narration_text,
            topic=topic,
            workdir=WORKDIR,
        )

        if not montage_result["video_path"]:
            send_alert("توقف الإنتاج: فشل تحميل كل الوسائط المتاحة للفيديو الطويل.", level="error")
            return

        long_video_path = montage_result["video_path"]

        try:
            thumbnail_path = thumbnail_generator.build_thumbnail(
                narration_text, topic, f"{WORKDIR}/thumbnail.jpg", is_short=False
            )
        except Exception as e:
            print(f"[WARNING] فشل توليد الغلاف المركّب: {e}. استخدام أول صورة مشهد كبديل.")
            fallback_images = [m for m in montage_result["media_items"] if m["type"] == "image"]
            thumbnail_path = fallback_images[0]["localPath"] if fallback_images else None

        seo_metadata = seo_optimizer.build_seo_metadata(topic, long_script)
        results = publish.publish_pair(
            long_video_path=long_video_path,
            long_meta=seo_metadata,
            long_thumbnail=thumbnail_path,
        )
        video_id = results["long_id"]
        sheets_client.append_row(SPREADSHEET_ID, config.Paths().sheets_daily_log, [video_id, seo_metadata["title"], "published"])

    except Exception as e:
        alert_step_failed("daily_pipeline", e)
        raise

if __name__ == "__main__":
    run()
