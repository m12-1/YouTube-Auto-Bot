"""
shorts_pipeline.py
المسار النشط الوحيد بالمرحلة الحالية (شورتس فقط، حسب قرارك). نفس منطق
daily_pipeline.py لكن مبسّط لمسار الشورت فقط، بدون الفيديو الطويل. يُستدعى
من shorts_pipeline.yml بالوقت المتفق عليه (04:00 بتوقيت بغداد).
"""
import os
import json

from scripts import config, sheets_client, script_writer, quality_gate
from scripts import voice_and_captions, asset_fetcher, thumbnail_generator
from scripts import seo_optimizer, publish
from scripts.daily_pipeline import render_video_via_remotion
from scripts.telegram_alerts import send_alert, alert_step_failed

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKDIR = "pipeline_output"


def run():
    if not sheets_client.is_system_enabled(SPREADSHEET_ID):
        print("النظام متوقف عبر System_Control. تخطي.")
        return

    os.makedirs(WORKDIR, exist_ok=True)

    try:
        # 1) قراءة آخر موضوع مختار من trend_scanner
        trend_records = sheets_client.get_all_records(SPREADSHEET_ID, config.Paths().sheets_trend_log)
        if not trend_records:
            send_alert("لا يوجد موضوع بـ Trend_Log لبدء الإنتاج اليوم.", level="warning")
            return
        topic = trend_records[-1]["core_topic"]

        # 2) سكربت الشورت + Quality Gate (فيه الفحص الصارم لقائمة الحظر أولاً)
        short_script = script_writer.write_short_script(topic)
        evaluation = quality_gate.evaluate(short_script["narration"])
        if not evaluation["passed"]:
            # محاولة ثانية وأخيرة بموضوع مختلف قليلاً لو فشل بسبب الحظر
            short_script = script_writer.write_short_script(topic)
            evaluation = quality_gate.evaluate(short_script["narration"])
            if not evaluation["passed"]:
                send_alert("توقف إنتاج الشورت اليوم: رسب بـ Quality Gate مرتين.", level="error")
                return

        # 3) الصوت + الكابشن
        audio_path = f"{WORKDIR}/short_audio.mp3"
        captions_path = f"{WORKDIR}/short_captions.json"
        voice_and_captions.generate_voice_and_captions(
            short_script["narration"], audio_path, captions_path
        )

        # 4) الصور (يُراعى بها اختيار صور تناسب الاتجاه العمودي، راجع asset_fetcher)
        image_paths = []
        urls = asset_fetcher.get_images_for_scene(short_script["visual_keywords"], target_count=4)
        for i, url in enumerate(urls):
            if url == "PLACEHOLDER_NO_IMAGE_FOUND":
                continue
            path = f"{WORKDIR}/short_scene_{i}.jpg"
            asset_fetcher.download_image(url, path)
            image_paths.append(path)

        # 5) الرندرة — Composition "ShortVideo" بدقة 1080x1920 مضمونة بالكود
        #    (هوامش الأمان الجانبية/السفلية مضبوطة بـ SyncedCaptions.tsx حسب
        #    config.SHORTS_SAFE_ZONE، ووضع الصور مُحسّن بـ KenBurnsImage.tsx)
        fake_long_script = {"hook": short_script["narration"], "scenes": [], "closing_cta": ""}
        short_video_path = render_video_via_remotion(
            fake_long_script, audio_path, captions_path, image_paths,
            composition_id="ShortVideo", out_path=f"{WORKDIR}/short_video.mp4",
            duration_seconds=config.SHORT_VIDEO_TARGET_SECONDS,
        )

        # 6) الغلاف (لقطة مصغّرة تلقائية من يوتيوب للشورتس عادة، لكن نجهزه احتياطاً)
        thumbnail_path = thumbnail_generator.build_thumbnail(
            short_script["narration"], topic, f"{WORKDIR}/short_thumbnail.jpg"
        )

        # 7) السيو — أهم خطوة حسب طلبك، تشمل بحث المنافسين الفعلي
        seo_metadata = seo_optimizer.build_seo_metadata(topic, fake_long_script)

        # 8) النشر
        video_id = publish.upload_video(
            short_video_path, seo_metadata["title"], seo_metadata["description"],
            seo_metadata["tags"], thumbnail_path=thumbnail_path, is_short=True,
        )

        # 9) تحديث السجل
        sheets_client.append_row(
            SPREADSHEET_ID, config.Paths().sheets_daily_log,
            [video_id, seo_metadata["title"], "published"],
        )

    except Exception as e:
        alert_step_failed("shorts_pipeline", e)
        raise


if __name__ == "__main__":
    run()
