"""
shorts_pipeline.py
المنسق الخاص بفيديوهات الشورت.
"""
import os
import json
import random
import shutil
import subprocess
import time
from scripts import config, sheets_client, script_writer, quality_gate
from scripts import voice_and_captions, asset_fetcher, thumbnail_generator
from scripts import seo_optimizer, publish
from scripts.telegram_alerts import send_alert, alert_step_failed

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKDIR = "pipeline_output"

def move_to_public(src_path: str) -> str:
    dest_dir = "remotion/public/assets"
    os.makedirs(dest_dir, exist_ok=True)
    if not src_path or not os.path.exists(src_path): return ""
    filename = os.path.basename(src_path)
    shutil.copy2(src_path, os.path.join(dest_dir, filename))
    return f"assets/{filename}"

def render_video_via_remotion(script_data, audio_path, captions_data, media_items, composition_id, out_path, duration_seconds):
    payload_path = os.path.abspath(f"{WORKDIR}/render_payload.json")
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({
            "script": script_data,
            "audioPath": move_to_public(audio_path),
            "captions": captions_data,
            "mediaItems": [{"type": m["type"], "src": move_to_public(m["localPath"]), "startFrame": m["startFrame"], "durationFrames": m["durationFrames"]} for m in media_items],
            "durationSeconds": duration_seconds,
            "width": 1080, "height": 1920, "fps": config.VIDEO_FPS,
        }, f, ensure_ascii=False)
    subprocess.run(["npx", "remotion", "render", composition_id, os.path.abspath(out_path), "--props", payload_path], cwd="remotion", check=True)
    return out_path

def run():
    if not sheets_client.is_system_enabled(SPREADSHEET_ID): return
    os.makedirs(WORKDIR, exist_ok=True)
    try:
        trend_records = sheets_client.get_all_records(SPREADSHEET_ID, config.Paths().sheets_trend_log)
        topic = trend_records[-1]["core_topic"]
        short_script = script_writer.write_short_script(topic)
        narration_text = script_writer.full_narration_text(short_script)
        
        evaluation = quality_gate.evaluate(narration_text)
        if not evaluation["passed"]:
            short_script = script_writer.write_short_script(topic)
            narration_text = script_writer.full_narration_text(short_script)
            if not quality_gate.evaluate(narration_text)["passed"]:
                send_alert("رسب السكربت مرتين.", level="error")
                return

        scene_narrations = [short_script["hook"]] + [s["narration"] for s in short_script["scenes"]] + [short_script["closing_cta"]]
        scene_keywords = [short_script["scenes"][0]["visual_keywords"]] + [s["visual_keywords"] for s in short_script["scenes"]] + [short_script["scenes"][-1]["visual_keywords"]]

        audio_path, captions_path = voice_and_captions.generate_voice_and_captions(narration_text, f"{WORKDIR}/short_audio.mp3", f"{WORKDIR}/short_captions.json")
        with open(captions_path, "r", encoding="utf-8") as f: word_events = json.load(f)
        scene_timings = voice_and_captions.map_scenes_to_timing(scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=55 * config.VIDEO_FPS)

        media_items = []
        for i, (keywords, timing) in enumerate(zip(scene_keywords, scene_timings)):
            time.sleep(4) # إبطاء وتيرة الطلبات لتجنب حظر Pixabay
            media_list = asset_fetcher.get_media_for_scene(keywords, target_count=1, is_short=True)
            if not media_list: continue
            
            item = media_list[0]
            local_path = asset_fetcher.download_video(item["url"], f"{WORKDIR}/s_{i}.mp4") if item["type"] == "video" else asset_fetcher.download_image(item["url"], f"{WORKDIR}/s_{i}.jpg")
            if not local_path: continue
            media_items.append({"type": item["type"], "localPath": local_path, "startFrame": timing["start_frame"], "durationFrames": timing["duration_frames"]})

        short_video_path = render_video_via_remotion(short_script, audio_path, word_events, media_items, "ShortVideo", f"{WORKDIR}/short_video.mp4", 55)
        seo_metadata = seo_optimizer.build_seo_metadata(topic, short_script)
        results = publish.publish_pair(short_video_path=short_video_path, short_meta=seo_metadata)
        sheets_client.append_row(SPREADSHEET_ID, config.Paths().sheets_daily_log, [results["short_id"], seo_metadata["title"], "published"])
    except Exception as e:
        alert_step_failed("shorts_pipeline", e)
        raise

if __name__ == "__main__":
    run()
