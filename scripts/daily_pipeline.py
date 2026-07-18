"""
daily_pipeline.py
المنسّق الرئيسي (Orchestrator) — يُستدعى من daily_pipeline.yml فجر كل يوم.
يربط: قراءة الترند المختار -> السكربت -> Quality Gate -> الصوت/الكابشن ->
الصور -> الرندرة (Remotion عبر subprocess) -> الغلاف -> السيو -> النشر.
"""
import os
import json
import subprocess

from scripts import config, sheets_client, script_writer, quality_gate
from scripts import voice_and_captions, asset_fetcher, thumbnail_generator
from scripts import seo_optimizer, publish
from scripts.telegram_alerts import send_alert, alert_step_failed

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKDIR = "pipeline_output"


def render_video_via_remotion(script_data: dict, audio_path: str, captions_path: str,
                               image_paths: list[str], composition_id: str,
                               out_path: str, duration_seconds: int):
    """يستدعي مشروع Remotion (Node.js) كعملية خارجية. راجع remotion/render.js."""
    payload_path = f"{WORKDIR}/render_payload.json"
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({
            "script": script_data,
            "audioPath": os.path.abspath(audio_path),
            "captionsPath": os.path.abspath(captions_path),
            "imagePaths": [os.path.abspath(p) for p in image_paths],
            "durationSeconds": duration_seconds,
            "width": config.VIDEO_WIDTH,
            "height": config.VIDEO_HEIGHT,
            "fps": config.VIDEO_FPS,
        }, f, ensure_ascii=False)

    subprocess.run(
        [
            "npx", "remotion", "render", composition_id,
            os.path.abspath(out_path),
            "--props", payload_path,
        ],
        cwd="remotion",
        check=True,
    )
    return out_path


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

        # 2) كتابة السكربت + Quality Gate (مع محاولة إعادة واحدة لو فشل)
        long_script = script_writer.write_long_script(topic)
        narration_text = script_writer.full_narration_text(long_script)
        evaluation = quality_gate.evaluate(narration_text)
        if not evaluation["passed"]:
            long_script = script_writer.write_long_script(topic)  # محاولة ثانية وأخيرة
            narration_text = script_writer.full_narration_text(long_script)
            evaluation = quality_gate.evaluate(narration_text)
            if not evaluation["passed"]:
                send_alert(f"توقف الإنتاج اليوم: السكربت رسب بـ Quality Gate مرتين.", level="error")
                return

        short_script = script_writer.write_short_script(topic)

        # 3) الصوت + الكابشن (للفيديو الطويل)
        audio_path = f"{WORKDIR}/long_audio.mp3"
        captions_path = f"{WORKDIR}/long_captions.json"
        voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

        # 4) الصور لكل مشهد
        image_paths = []
        for i, scene in enumerate(long_script["scenes"]):
            urls = asset_fetcher.get_images_for_scene(scene["visual_keywords"])
            for j, url in enumerate(urls):
                if url == "PLACEHOLDER_NO_IMAGE_FOUND":
                    continue
                path = f"{WORKDIR}/scene_{i}_{j}.jpg"
                asset_fetcher.download_image(url, path)
                image_paths.append(path)

        # 5) الرندرة (1080p مضمونة عبر إعدادات remotion.config + تحقق publish._verify_1080p)
        long_video_path = render_video_via_remotion(
            long_script, audio_path, captions_path, image_paths,
            composition_id="LongVideo", out_path=f"{WORKDIR}/long_video.mp4",
            duration_seconds=config.LONG_VIDEO_TARGET_SECONDS,
        )

        # 6) الغلاف
        thumbnail_path = thumbnail_generator.build_thumbnail(
            narration_text, topic, f"{WORKDIR}/thumbnail.jpg"
        )

        # 7) السيو (الجزء الأهم — راجع seo_optimizer.py)
        seo_metadata = seo_optimizer.build_seo_metadata(topic, long_script)

        # 8) النشر
        video_id = publish.publish_pair(
            long_video_path, seo_metadata, thumbnail_path, None, None
        )

        # 9) تحديث السجل
        sheets_client.append_row(
            SPREADSHEET_ID, config.Paths().sheets_daily_log,
            [video_id, seo_metadata["title"], "published"],
        )

    except Exception as e:
        alert_step_failed("daily_pipeline", e)
        raise


if __name__ == "__main__":
    run()
