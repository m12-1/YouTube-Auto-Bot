"""
shorts_pipeline.py
المنسق الخاص بفيديوهات الشورت - مزود بالتخطي الذكي لمشكلة رصيد توليد الصور (API).
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
    """يستدعي Remotion باستخدام المسارات المطلقة للرندرة."""
    payload_path = os.path.abspath(f"{WORKDIR}/render_payload.json")
    
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({
            "script": script_data,
            "audioPath": os.path.abspath(audio_path),
            "captionsPath": os.path.abspath(captions_path),
            "imagePaths": [os.path.abspath(p) for p in image_paths],
            "durationSeconds": duration_seconds,
            "width": 1080,
            "height": 1920,
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
        # 1) قراءة الموضوع
        trend_records = sheets_client.get_all_records(SPREADSHEET_ID, config.Paths().sheets_trend_log)
        if not trend_records:
            send_alert("لا يوجد موضوع بـ Trend_Log لبدء الإنتاج اليوم.", level="warning")
            return
        topic = trend_records[-1]["core_topic"]

        # 2) كتابة سكربت الشورت
        short_script = script_writer.write_short_script(topic)
        narration_text = short_script["narration"]

        # 3) الصوت + الكابشن
        audio_path = f"{WORKDIR}/short_audio.mp3"
        captions_path = f"{WORKDIR}/short_captions.json"
        voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

        # 4) الصور لكل مشهد (الشورت عادة مشهد واحد طويل أو نعامله ككتلة واحدة)
        image_paths = []
        urls = asset_fetcher.get_images_for_scene(short_script["visual_keywords"])
        for j, url in enumerate(urls):
            if url == "PLACEHOLDER_NO_IMAGE_FOUND":
                continue
            path = f"{WORKDIR}/short_scene_{j}.jpg"
            asset_fetcher.download_image(url, path)
            image_paths.append(path)

        # 5) الرندرة
        short_video_path = render_video_via_remotion(
            short_script, audio_path, captions_path, image_paths,
            composition_id="ShortVideo", out_path=f"{WORKDIR}/short_video.mp4",
            duration_seconds=55,
        )

        # 6) الغلاف (التخطي الذكي: إذا فشل Gemini بسبب الرصيد المجاني، نستخدم أول صورة من الفيديو)
        try:
            thumbnail_path = thumbnail_generator.build_thumbnail(
                narration_text, topic, f"{WORKDIR}/thumbnail.jpg"
            )
        except Exception as e:
            print(f"[WARNING] فشل توليد الغلاف عبر Gemini API (رصيد الصور مستنفد). جاري استخدام صورة بديلة...")
            thumbnail_path = image_paths[0] if image_paths else None

        # 7) السيو
        seo_metadata = seo_optimizer.build_seo_metadata(topic, short_script)

        # 8) النشر
        video_id = publish.publish_pair(
            None, seo_metadata, None, short_video_path, thumbnail_path
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
