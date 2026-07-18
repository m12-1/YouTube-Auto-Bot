"""
daily_pipeline.py
المنسّق الرئيسي (Orchestrator) — تم إصلاح مسارات الملفات للرندرة.
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
    """يستدعي مشروع Remotion (Node.js) بمسارات مطلقة لضمان دقة الرندرة."""
    # التأكد من المسارات المطلقة
    payload_path = os.path.abspath(f"{WORKDIR}/render_payload.json")
    
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

# ... (بقية الدالة run كما هي)
