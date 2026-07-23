"""
shorts_pipeline.py
المنسق الخاص بفيديوهات الشورت. المونتاج الفعلي (صوت/كابشن/وسائط/رندرة)
انتقل بالكامل إلى scripts/video_montage.py — هذا الملف يبقى مسؤولاً فقط عن
تسلسل الخطوات العام: اختيار الموضوع، كتابة السكربت، Quality Gate، ثم
المونتاج، ثم الغلاف والسيو والنشر والتسجيل بالجدول.
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

        short_script = script_writer.write_short_script(topic)
        narration_text = script_writer.full_narration_text(short_script)

        evaluation = quality_gate.evaluate(narration_text)
        if not evaluation["passed"]:
            print("[QUALITY GATE] السكربت رسب في الفحص الأول. جاري محاولة كتابة سكربت جديد...")
            short_script = script_writer.write_short_script(topic)
            narration_text = script_writer.full_narration_text(short_script)
            evaluation = quality_gate.evaluate(narration_text)
            if not evaluation["passed"]:
                send_alert("توقف إنتاج الشورت: السكربت رسب بـ Quality Gate مرتين.", level="error")
                return

        montage_result = video_montage.build_short_montage(
            short_script=short_script,
            narration_text=narration_text,
            topic=topic,
            workdir=WORKDIR,
        )

        if not montage_result["video_path"]:
            send_alert("توقف إنتاج الشورت: فشل تحميل كل الوسائط المتاحة.", level="error")
            return

        short_video_path = montage_result["video_path"]

        try:
            thumbnail_path = thumbnail_generator.build_thumbnail(
                narration_text, topic, f"{WORKDIR}/thumbnail.jpg", is_short=True
            )
        except Exception as e:
            print(f"[WARNING] فشل توليد الغلاف المركّب: {e}. استخدام أول صورة مشهد كبديل.")
            fallback_images = [m for m in montage_result["media_items"] if m["type"] == "image"]
            thumbnail_path = fallback_images[0]["localPath"] if fallback_images else None

        seo_metadata = seo_optimizer.build_seo_metadata(topic, short_script)

        results = publish.publish_pair(
            short_video_path=short_video_path,
            short_meta=seo_metadata,
            short_thumbnail=thumbnail_path,
        )
        video_id = results["short_id"]

        try:
            sheets_client.append_row(
                SPREADSHEET_ID, config.Paths().sheets_daily_log,
                [video_id, seo_metadata["title"], "published"],
            )
        except Exception as e:
            print(f"[WARNING] تم نشر الفيديو بنجاح لكن فشل تسجيله في Google Sheets: {e}")

    except Exception as e:
        alert_step_failed("shorts_pipeline", e)
        raise

if __name__ == "__main__":
    run()
