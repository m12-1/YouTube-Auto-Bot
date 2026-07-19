"""
daily_pipeline.py

نفس تحديث المونتاج المطبّق بـ shorts_pipeline.py: مزيج فيديو/صور لكل مشهد
بدل صور فقط، وتوقيت تبديل الوسائط مرتبط بزمن كل مشهد الحقيقي بالصوت بدل
فترة ثابتة. الفرق الوحيد هنا: السكربت الطويل أصلاً كان مقسّم مشاهد
(scenes مع visual_keywords لكل مشهد)، فما احتجنا تعديل script_writer لهذا
المسار — فقط طريقة جلب الوسائط وربطها بالتوقيت.
"""
import os
import json
import random
import shutil
import subprocess
from scripts import config, sheets_client, script_writer, quality_gate
from scripts import voice_and_captions, asset_fetcher, thumbnail_generator
from scripts import seo_optimizer, publish
from scripts.telegram_alerts import send_alert, alert_step_failed

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKDIR = "pipeline_output"

def move_to_public(src_path: str) -> str:
    """ينسخ الملفات لمجلد public الخاص بـ Remotion ويُرجع مساراً نسبياً."""
    dest_dir = "remotion/public/assets"
    os.makedirs(dest_dir, exist_ok=True)
    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)
    shutil.copy2(src_path, dest_path)
    # نرجع المسار النسبي فقط بدون / في البداية
    return f"assets/{filename}"

def render_video_via_remotion(script_data: dict, audio_path: str, captions_path: str,
                                media_items: list[dict], composition_id: str,
                                out_path: str, duration_seconds: int):
    payload_path = os.path.abspath(f"{WORKDIR}/render_payload.json")

    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({
            "script": script_data,
            "audioPath": move_to_public(audio_path),
            "captionsPath": move_to_public(captions_path),
            "mediaItems": [
                {
                    "type": m["type"],
                    "src": move_to_public(m["localPath"]),
                    "startFrame": m["startFrame"],
                    "durationFrames": m["durationFrames"],
                }
                for m in media_items
            ],
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

        audio_path = f"{WORKDIR}/long_audio.mp3"
        captions_path = f"{WORKDIR}/long_captions.json"
        voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

        with open(captions_path, "r", encoding="utf-8") as f:
            word_events = json.load(f)

        scene_narrations = (
            [long_script["hook"]]
            + [s["narration"] for s in long_script["scenes"]]
            + [long_script["closing_cta"]]
        )
        scene_keywords = (
            [long_script["scenes"][0]["visual_keywords"]]
            + [s["visual_keywords"] for s in long_script["scenes"]]
            + [long_script["scenes"][-1]["visual_keywords"]]
        )
        total_frames = config.LONG_VIDEO_TARGET_SECONDS * config.VIDEO_FPS
        scene_timings = voice_and_captions.map_scenes_to_timing(
            scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=total_frames
        )

        media_items = []
        for i, (keywords, timing) in enumerate(zip(scene_keywords, scene_timings)):
            prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
            media_list = asset_fetcher.get_media_for_scene(
                keywords, target_count=1, is_short=False, prefer_video=prefer_video
            )
            if not media_list:
                continue

            item = media_list[0]
            if item["type"] == "video":
                local_path = asset_fetcher.download_video(item["url"], f"{WORKDIR}/scene_{i}.mp4")
            else:
                local_path = asset_fetcher.download_image(item["url"], f"{WORKDIR}/scene_{i}.jpg")

            if not local_path:
                continue

            media_items.append({
                "type": item["type"],
                "localPath": local_path,
                "startFrame": timing["start_frame"],
                "durationFrames": timing["duration_frames"],
            })

        if not media_items:
            send_alert("توقف الإنتاج: فشل تحميل كل الوسائط المتاحة للفيديو الطويل.", level="error")
            return

        long_video_path = render_video_via_remotion(
            long_script, audio_path, captions_path, media_items,
            composition_id="LongVideo", out_path=f"{WORKDIR}/long_video.mp4",
            duration_seconds=config.LONG_VIDEO_TARGET_SECONDS,
        )

        try:
            thumbnail_path = thumbnail_generator.build_thumbnail(
                narration_text, topic, f"{WORKDIR}/thumbnail.jpg", is_short=False
            )
        except Exception as e:
            print(f"[WARNING] فشل توليد الغلاف المركّب: {e}. استخدام أول صورة مشهد كبديل.")
            fallback_images = [m for m in media_items if m["type"] == "image"]
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
