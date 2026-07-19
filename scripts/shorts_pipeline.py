"""
shorts_pipeline.py
المنسق الخاص بفيديوهات الشورت - حل نهائي لمشكلة المسارات + تخطي الغلاف.

تحديث "المونتاج الاحترافي" (بدل عرض الشرائح):
- السكربت صار مقسّم مشاهد (script_writer الجديد)، فكل مشهد له نص + كلمات
  مفتاحية خاصة به بدل كلمة مفتاحية واحدة عامة للفيديو كامل.
- كل مشهد يجلب "وسيط" مختلط (فيديو غالباً، صورة كـ fallback) عبر
  asset_fetcher.get_media_for_scene بدل صورة ثابتة فقط.
- توقيت تبديل الوسائط صار مرتبط فعلياً بزمن كل مشهد بالصوت الحقيقي عبر
  voice_and_captions.map_scenes_to_timing، بدل تبديل كل X ثانية ثابتة بلا
  علاقة بالمحتوى المسموع — هذا أهم إصلاح بخصوص شكل "السبام".
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
    return f"assets/{filename}"

def render_video_via_remotion(script_data: dict, audio_path: str, captions_path: str,
                                media_items: list[dict], composition_id: str,
                                out_path: str, duration_seconds: int):
    """
    media_items: [{"type": "video"|"image", "localPath": "...", "startFrame": int,
                    "durationFrames": int}, ...] — مرتبة زمنياً، كل عنصر يمثل
    مشهد واحد من السكربت بمدته الحقيقية بالصوت (وليس فترة ثابتة).
    """
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
        trend_records = sheets_client.get_all_records(SPREADSHEET_ID, config.Paths().sheets_trend_log)
        if not trend_records:
            send_alert("لا يوجد موضوع بـ Trend_Log لبدء الإنتاج اليوم.", level="warning")
            return
        topic = trend_records[-1]["core_topic"]

        short_script = script_writer.write_short_script(topic)
        # السكربت الجديد مقسّم مشاهد (hook/scenes/closing_cta) مثل الطويل
        # تماماً، فنبني نص السرد الكامل ونص كل مشهد بنفس الدالة المشتركة
        narration_text = script_writer.full_narration_text(short_script)
        
        # === إضافة فحص Quality Gate ===
        evaluation = quality_gate.evaluate(narration_text)
        if not evaluation["passed"]:
            print(f"[QUALITY GATE] السكربت رسب في الفحص الأول. جاري محاولة كتابة سكربت جديد...")
            short_script = script_writer.write_short_script(topic)
            narration_text = script_writer.full_narration_text(short_script)
            evaluation = quality_gate.evaluate(narration_text)
            if not evaluation["passed"]:
                send_alert("توقف إنتاج الشورت: السكربت رسب بـ Quality Gate مرتين.", level="error")
                return
        # === نهاية فحص Quality Gate ===

        scene_narrations = (
            [short_script["hook"]]
            + [s["narration"] for s in short_script["scenes"]]
            + [short_script["closing_cta"]]
        )
        scene_keywords = (
            [short_script["scenes"][0]["visual_keywords"]]  # الهوك يشارك كلمات أول مشهد
            + [s["visual_keywords"] for s in short_script["scenes"]]
            + [short_script["scenes"][-1]["visual_keywords"]]  # الخاتمة تشارك كلمات آخر مشهد
        )

        audio_path = f"{WORKDIR}/short_audio.mp3"
        captions_path = f"{WORKDIR}/short_captions.json"
        voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

        with open(captions_path, "r", encoding="utf-8") as f:
            word_events = json.load(f)

        total_frames = 55 * config.VIDEO_FPS
        scene_timings = voice_and_captions.map_scenes_to_timing(
            scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=total_frames
        )

        media_items = []
        for i, (keywords, timing) in enumerate(zip(scene_keywords, scene_timings)):
            prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
            media_list = asset_fetcher.get_media_for_scene(
                keywords, target_count=1, is_short=True, prefer_video=prefer_video
            )
            if not media_list:
                continue  # نتخطى المشهد بدل إيقاف كل الفيديو لو فشلت كل المحاولات لكلماته

            item = media_list[0]
            if item["type"] == "video":
                local_path = asset_fetcher.download_video(item["url"], f"{WORKDIR}/short_scene_{i}.mp4")
            else:
                local_path = asset_fetcher.download_image(item["url"], f"{WORKDIR}/short_scene_{i}.jpg")

            if not local_path:
                continue

            media_items.append({
                "type": item["type"],
                "localPath": local_path,
                "startFrame": timing["start_frame"],
                "durationFrames": timing["duration_frames"],
            })

        if not media_items:
            send_alert("توقف إنتاج الشورت: فشل تحميل كل الوسائط المتاحة (فيديو وصور).", level="error")
            return

        short_video_path = render_video_via_remotion(
            short_script, audio_path, captions_path, media_items,
            composition_id="ShortVideo", out_path=f"{WORKDIR}/short_video.mp4",
            duration_seconds=55,
        )

        try:
            thumbnail_path = thumbnail_generator.build_thumbnail(
                narration_text, topic, f"{WORKDIR}/thumbnail.jpg", is_short=True
            )
        except Exception as e:
            print(f"[WARNING] فشل توليد الغلاف المركّب: {e}. استخدام أول صورة مشهد كبديل.")
            fallback_images = [m for m in media_items if m["type"] == "image"]
            thumbnail_path = fallback_images[0]["localPath"] if fallback_images else None

        seo_metadata = seo_optimizer.build_seo_metadata(topic, short_script)

        # هذا هو الإصلاح الأساسي لخطأ NoneType الذي واجهته: نمرر شورت فقط
        # (long_video_path=None ضمنياً، publish_pair الجديدة تتجاهله بأمان
        # بدل ما تحاول رفعه وتنهار)
        results = publish.publish_pair(
            short_video_path=short_video_path,
            short_meta=seo_metadata,
            short_thumbnail=thumbnail_path,
        )
        video_id = results["short_id"]

        sheets_client.append_row(
            SPREADSHEET_ID, config.Paths().sheets_daily_log,
            [video_id, seo_metadata["title"], "published"],
        )

    except Exception as e:
        alert_step_failed("shorts_pipeline", e)
        raise

if __name__ == "__main__":
    run()
