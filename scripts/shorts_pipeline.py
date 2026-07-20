"""
shorts_pipeline.py
المنسق الخاص بفيديوهات الشورت - حل نهائي لمشكلة المسارات + تخطي الغلاف.
"""
import os
import json
import random
import shutil
import subprocess
import time
from mutagen.mp3 import MP3
from scripts import config, sheets_client, script_writer, quality_gate
from scripts import voice_and_captions, asset_fetcher, thumbnail_generator
from scripts import seo_optimizer, publish
from scripts.telegram_alerts import send_alert, alert_step_failed

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
WORKDIR = "pipeline_output"

def move_to_public(src_path: str) -> str:
    dest_dir = "remotion/public/assets"
    os.makedirs(dest_dir, exist_ok=True)
    if not src_path or not os.path.exists(src_path):
        return ""
    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)
    shutil.copy2(src_path, dest_path)
    return f"assets/{filename}"

def render_video_via_remotion(script_data: dict, audio_path: str, captions_data: list,
                                media_items: list[dict], composition_id: str,
                                out_path: str, duration_seconds: int):
    payload_path = os.path.abspath(f"{WORKDIR}/render_payload.json")

    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({
            "script": script_data,
            "audioPath": move_to_public(audio_path),
            "captions": captions_data,
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

    print(f"[REMOTION] جاري الرندرة، تم تمرير {len(media_items)} مشاهد و {len(captions_data)} كلمة.")

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
        narration_text = script_writer.full_narration_text(short_script)
        
        evaluation = quality_gate.evaluate(narration_text)
        if not evaluation["passed"]:
            print(f"[QUALITY GATE] السكربت رسب في الفحص الأول. جاري محاولة كتابة سكربت جديد...")
            short_script = script_writer.write_short_script(topic)
            narration_text = script_writer.full_narration_text(short_script)
            evaluation = quality_gate.evaluate(narration_text)
            if not evaluation["passed"]:
                send_alert("توقف إنتاج الشورت: السكربت رسب بـ Quality Gate مرتين.", level="error")
                return

        scenes = short_script.get("scenes", [])
        fallback_kw = ["nature", "background", "sky"]

        scene_narrations = (
            [short_script.get("hook", "")]
            + [s.get("narration", "") for s in scenes]
            + [short_script.get("closing_cta", "")]
        )
        scene_keywords = (
            [scenes[0].get("visual_keywords", fallback_kw) if scenes else fallback_kw]
            + [s.get("visual_keywords", fallback_kw) for s in scenes]
            + [scenes[-1].get("visual_keywords", fallback_kw) if scenes else fallback_kw]
        )

        audio_path = f"{WORKDIR}/short_audio.mp3"
        captions_path = f"{WORKDIR}/short_captions.json"
        voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

        with open(captions_path, "r", encoding="utf-8") as f:
            word_events = json.load(f)

        try:
            audio_info = MP3(audio_path)
            audio_duration_sec = min(audio_info.info.length, 59)
        except Exception as e:
            print(f"[WARNING] فشل حساب مدة الصوت: {e}. سيتم افتراض 55 ثانية.")
            audio_duration_sec = 55

        total_frames = round(audio_duration_sec * config.VIDEO_FPS)

        scene_timings = voice_and_captions.map_scenes_to_timing(
            scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=total_frames
        )

        media_items = []
        for i, (keywords, timing, narration) in enumerate(zip(scene_keywords, scene_timings, scene_narrations)):
            prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
            # جلب 3 خيارات لاختيار الأفضل بصرياً
            media_list = asset_fetcher.get_media_for_scene(
                keywords, target_count=3, is_short=True, prefer_video=prefer_video,
                topic_context=topic
            )
            
            local_path = None
            media_type = "image"
            
            for item in media_list:
                temp_type = item["type"]
                temp_path = f"{WORKDIR}/short_scene_{i}"
                if temp_type == "video":
                    temp_path += ".mp4"
                    downloaded = asset_fetcher.download_video(item["url"], temp_path)
                else:
                    temp_path += ".jpg"
                    downloaded = asset_fetcher.download_image(item["url"], temp_path)
                
                if downloaded:
                    # تحقق بصري باستخدام Gemini
                    if asset_fetcher.verify_media_file(temp_path, narration):
                        local_path = temp_path
                        media_type = temp_type
                        break # نجح الفحص، نعتمد هذا المقطع
                    else:
                        # الفحص فشل، نحذف الملف ونجرب المقطع التالي
                        try:
                            os.remove(temp_path)
                        except:
                            pass
            
            if not local_path:
                print(f"[WARNING] فشل تحميل وسائط المشهد {i}. سيتم تمديد المشهد السابق لتغطية الفراغ.")
                if media_items:
                    media_items[-1]["durationFrames"] += timing["duration_frames"]
                continue

            media_items.append({
                "type": media_type,
                "localPath": local_path,
                "startFrame": timing["start_frame"],
                "durationFrames": timing["duration_frames"],
            })
            time.sleep(4) 

        if not media_items:
            send_alert("توقف إنتاج الشورت: فشل تحميل كل الوسائط المتاحة.", level="error")
            return

        print(f"[DEBUG] تم تجهيز {len(media_items)} مشاهد للرندرة بنجاح.")

        short_video_path = render_video_via_remotion(
            short_script, audio_path, word_events, media_items,
            composition_id="ShortVideo", out_path=f"{WORKDIR}/short_video.mp4",
            duration_seconds=audio_duration_sec,
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
