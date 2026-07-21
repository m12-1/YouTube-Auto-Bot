"""
video_montage.py
مهمة هذا الملف فقط: "المونتاج" — من النص السردي الجاهز إلى فيديو نهائي:
  1) توليد الصوت + الكابشن المتزامن كلمة-بكلمة (voice_and_captions.py)
  2) حساب توقيت كل مشهد الحقيقي على الصوت
  3) جلب/تنزيل وسائط كل مشهد والتحقق من تطابقها مع النص (asset_fetcher.py
     + media_relevance_checker.py)
  4) استدعاء Remotion لدمج الصوت + الكابشن + الوسائط بفيديو نهائي واحد

تم فصل هذه الخطوات هنا (كانت متداخلة داخل shorts_pipeline.py/daily_pipeline.py)
حسب طلب فصل كل مهمة بملف مستقل لتسهيل الصيانة. shorts_pipeline.py و
daily_pipeline.py أصبحا الآن مجرد "منسّقين" يستدعيان دوال هذا الملف.
"""
import os
import json
import random
import shutil
import subprocess
import time

from mutagen.mp3 import MP3

from scripts import config, asset_fetcher, voice_and_captions, media_relevance_checker

# صورة احتياطية عامة تُستخدم فقط لو رفض فلتر التطابق كل خيارات مشهد معيّن،
# لتفادي شاشة سوداء بدل تعطيل الإنتاج بالكامل
GENERIC_FALLBACK_IMAGE_URL = "https://cdn.pixabay.com/photo/2020/09/23/19/40/gaming-5596956_1280.jpg"


def move_to_public(src_path: str) -> str:
    """ينسخ الملف لمجلد public الخاص بـ Remotion ويُرجع مساراً نسبياً."""
    dest_dir = "remotion/public/assets"
    os.makedirs(dest_dir, exist_ok=True)
    if not src_path or not os.path.exists(src_path):
        return ""
    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)
    shutil.copy2(src_path, dest_path)
    return f"assets/{filename}"


def _render_via_remotion(payload: dict, composition_id: str, out_path: str, workdir: str, log_label: str):
    payload_path = os.path.abspath(f"{workdir}/render_payload.json")
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"[MONTAGE] [{log_label}] جاري الرندرة عبر Remotion "
          f"({len(payload.get('mediaItems', []))} مشهد وسائط)...")
    try:
        subprocess.run(
            ["npx", "remotion", "render", composition_id, os.path.abspath(out_path), "--props", payload_path],
            cwd="remotion",
            check=True,
        )
    except Exception as e:
        print(f"[MONTAGE ERROR] [{log_label}] فشلت رندرة Remotion: {e}")
        raise
    return out_path


def _resolve_scene_media(scene_keywords, scene_timings, scene_narrations, workdir,
                          file_prefix, is_short, topic_context, verify, sleep_between_seconds):
    """يجلب وسيطاً واحداً مناسباً لكل مشهد (فيديو أو صورة)، يحمّله فعلياً،
    ثم (اختيارياً) يتحقق من تطابقه مع النص السردي قبل اعتماده نهائياً."""
    media_items = []
    target_count = 3 if verify else 1

    for i, (keywords, timing, narration) in enumerate(zip(scene_keywords, scene_timings, scene_narrations)):
        prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
        media_list = asset_fetcher.get_media_for_scene(
            keywords, target_count=target_count, is_short=is_short,
            prefer_video=prefer_video, topic_context=topic_context,
        )

        local_path = None
        media_type = "image"

        for item in media_list:
            temp_type = item["type"]
            temp_path = f"{workdir}/{file_prefix}_{i}"
            if temp_type == "video":
                temp_path += ".mp4"
                downloaded = asset_fetcher.download_video(item["url"], temp_path)
            else:
                temp_path += ".jpg"
                downloaded = asset_fetcher.download_image(item["url"], temp_path)

            if not downloaded:
                continue

            if verify:
                if media_relevance_checker.verify_media_file(temp_path, narration):
                    local_path = temp_path
                    media_type = temp_type
                    break
                else:
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
            else:
                local_path = temp_path
                media_type = temp_type
                break

        if not local_path and verify:
            print(f"[MONTAGE WARNING] رفض فلتر التطابق جميع وسائط المشهد {i}. سيتم استخدام صورة بديلة (Fallback).")
            local_path = asset_fetcher.download_image(GENERIC_FALLBACK_IMAGE_URL, f"{workdir}/{file_prefix}_{i}_fallback.jpg")
            media_type = "image"

        if not local_path:
            print(f"[MONTAGE ERROR] فشل تام في توفير وسائط المشهد {i}.")
            if media_items:
                print("[MONTAGE] سيتم تمديد مدة المشهد السابق بدلاً من هذا المشهد.")
                media_items[-1]["durationFrames"] += timing["duration_frames"]
            continue

        media_items.append({
            "type": media_type,
            "localPath": local_path,
            "startFrame": timing["start_frame"],
            "durationFrames": timing["duration_frames"],
        })

        if sleep_between_seconds:
            time.sleep(sleep_between_seconds)

    return media_items


def build_short_montage(short_script: dict, narration_text: str, scene_narrations: list[str],
                         scene_keywords: list[list[str]], topic: str, workdir: str) -> dict:
    """
    مونتاج فيديو الشورت الكامل. يرجع dict:
    {"video_path": str|None, "audio_path": str, "duration_seconds": float,
     "media_items": list[dict]}
    video_path يكون None لو فشل تحميل كل الوسائط (نفس سلوك الأصل: يتوقف
    الاستدعاء الأعلى وينبّه بدل الرندرة على فيديو فارغ).
    """
    audio_path = f"{workdir}/short_audio.mp3"
    captions_path = f"{workdir}/short_captions.json"
    voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

    with open(captions_path, "r", encoding="utf-8") as f:
        word_events = json.load(f)

    try:
        audio_info = MP3(audio_path)
        audio_duration_sec = min(audio_info.info.length, 59)
    except Exception as e:
        print(f"[MONTAGE WARNING] فشل حساب مدة الصوت: {e}. سيتم افتراض 55 ثانية.")
        audio_duration_sec = 55

    total_frames = round(audio_duration_sec * config.VIDEO_FPS)
    scene_timings = voice_and_captions.map_scenes_to_timing(
        scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=total_frames
    )

    # Pixabay API يفشل بخطأ 400 لو كان الاستعلام طويلاً جداً؛ نأخذ فقط أول
    # 3-4 كلمات من الموضوع لإعطاء سياق كافٍ بدون كسر الـ API
    short_topic_context = " ".join(topic.split()[:4]) if topic else ""

    media_items = _resolve_scene_media(
        scene_keywords, scene_timings, scene_narrations, workdir,
        file_prefix="short_scene", is_short=True, topic_context=short_topic_context,
        verify=True, sleep_between_seconds=4,
    )

    if not media_items:
        return {"video_path": None, "audio_path": audio_path, "duration_seconds": audio_duration_sec, "media_items": []}

    print(f"[MONTAGE] تم تجهيز {len(media_items)} مشاهد شورت للرندرة بنجاح.")

    payload = {
        "script": short_script,
        "audioPath": move_to_public(audio_path),
        "captions": word_events,
        "mediaItems": [
            {
                "type": m["type"],
                "src": move_to_public(m["localPath"]),
                "startFrame": m["startFrame"],
                "durationFrames": m["durationFrames"],
            }
            for m in media_items
        ],
        "durationSeconds": audio_duration_sec,
        "width": config.SHORT_WIDTH,
        "height": config.SHORT_HEIGHT,
        "fps": config.VIDEO_FPS,
    }

    video_path = _render_via_remotion(
        payload, composition_id="ShortVideo", out_path=f"{workdir}/short_video.mp4",
        workdir=workdir, log_label="SHORT",
    )

    return {"video_path": video_path, "audio_path": audio_path, "duration_seconds": audio_duration_sec, "media_items": media_items}


def build_long_montage(long_script: dict, narration_text: str, topic: str, workdir: str) -> dict:
    """
    مونتاج الفيديو الطويل الكامل (المسار المستقبلي المعطّل حالياً بالجدولة).
    نفس منطق daily_pipeline.py الأصلي بدون أي تغيير سلوكي: بدون فحص تطابق
    بصري لكل مشهد (لم يكن مفعّلاً بالنسخة الأصلية لهذا المسار تحديداً)،
    وبدون فاصل انتظار بين المشاهد. الفرق الوحيد المكتسب تلقائياً: توزيع
    جلب الوسائط بين Pixabay و Pexels عبر asset_fetcher.py المشترك.
    """
    audio_path = f"{workdir}/long_audio.mp3"
    captions_path = f"{workdir}/long_captions.json"
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

    media_items = _resolve_scene_media(
        scene_keywords, scene_timings, scene_narrations, workdir,
        file_prefix="scene", is_short=False, topic_context="",
        verify=False, sleep_between_seconds=0,
    )

    if not media_items:
        return {"video_path": None, "audio_path": audio_path, "duration_seconds": config.LONG_VIDEO_TARGET_SECONDS, "media_items": []}

    payload = {
        "script": long_script,
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
        "durationSeconds": config.LONG_VIDEO_TARGET_SECONDS,
        "width": config.VIDEO_WIDTH,
        "height": config.VIDEO_HEIGHT,
        "fps": config.VIDEO_FPS,
    }

    video_path = _render_via_remotion(
        payload, composition_id="LongVideo", out_path=f"{workdir}/long_video.mp4",
        workdir=workdir, log_label="LONG",
    )

    return {"video_path": video_path, "audio_path": audio_path, "duration_seconds": config.LONG_VIDEO_TARGET_SECONDS, "media_items": media_items}
