"""
video_montage.py
مهمة هذا الملف فقط: "المونتاج" — من النص السردي الجاهز إلى فيديو نهائي:
  1) توليد الصوت + الكابشن المتزامن كلمة-بكلمة (voice_and_captions.py)
  2) حساب توقيت كل مشهد الحقيقي على الصوت، ثم تقسيمه لعدة "وحدات بصرية"
     (visual units) حسب ما حدده Gemini بكل مشهد (راجع _build_scene_entries
     و_expand_scene_to_visual_units) — بدل صورة/فيديو واحد ثابت يغطي كامل
     مدة المشهد حتى لو تغيّر الموضوع المشروح داخل نفس المشهد.
  3) جلب/تنزيل وسائط كل وحدة بصرية بالترتيب الصارم (لا ننتقل للتالية إلا
     بعد حسم الحالية)، والتحقق من تطابقها مع النص السردي (asset_fetcher.py
     + media_relevance_checker.py)، مع طلب بدائل من Gemini لو رُفضت كل
     المرشحين (راجع _resolve_visual_units وscript_writer.suggest_replacement_visual)
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

from scripts import config, asset_fetcher, voice_and_captions, media_relevance_checker, script_writer

# كلمات احتياطية عامة أخيرة (بحث حي فعلي عبر asset_fetcher، وليس رابطاً
# ثابتاً قد يموت لاحقاً كما كان بالنسخة السابقة GENERIC_FALLBACK_IMAGE_URL)
GENERIC_LAST_RESORT_KEYWORDS = ["cinematic background", "abstract technology", "nature footage"]

# كم مرة نطلب من Gemini كلمات بديلة لقطعة بصرية فشلت كل مرشحيها الثلاثة،
# قبل اللجوء نهائياً للاحتياط العام (راجع _resolve_visual_units)
MAX_GEMINI_REPLACEMENTS = 2


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


def _build_scene_entries(script: dict) -> list[dict]:
    """
    يحوّل السكربت الكامل (hook + scenes + closing_cta) إلى قائمة موحّدة من
    {"narration": str, "visuals": [{"keywords": [...], "duration_seconds": float}, ...]}.

    ملاحظة: hook و closing_cta بالسكيمة الحالية نصّان عاديان (بدون حقل
    visuals خاص بهم) حتى لا نكسر أي كود آخر يتعامل معهم كنصوص (seo_optimizer،
    thumbnail_generator، quality_gate...الخ) — فنعطيهم نفس منطق النسخة
    الأصلية: يستعيران visuals أول/آخر مشهد حقيقي على التوالي.
    """
    scenes = script.get("scenes", [])
    fallback_visuals = [{"keywords": ["nature", "background"], "duration_seconds": 3.0}]

    hook_visuals = (scenes[0].get("visuals") if scenes else None) or fallback_visuals
    cta_visuals = (scenes[-1].get("visuals") if scenes else None) or fallback_visuals

    entries = [{"narration": script.get("hook", ""), "visuals": hook_visuals}]
    for s in scenes:
        entries.append({"narration": s.get("narration", ""), "visuals": s.get("visuals") or fallback_visuals})
    entries.append({"narration": script.get("closing_cta", ""), "visuals": cta_visuals})
    return entries


def _expand_scene_to_visual_units(scene_entries: list[dict], scene_timings: list[dict]) -> list[dict]:
    """
    يحوّل كل مشهد (له مدة زمنية حقيقية واحدة، مأخوذة من توقيت الصوت الفعلي)
    إلى عدة "وحدات بصرية" (Visual Units) حسب ما حدده Gemini بحقل visuals —
    هذا يحل مشكلة "الصورة تبقى ثابتة رغم تغيّر الموضوع"، لأن كل فكرة/جملة
    فرعية بنفس المشهد تحصل الآن على صورتها/فيديوهاتها الخاصة بدل صورة واحدة
    تغطي كامل مدة المشهد.

    نِسَب duration_seconds التي حددها Gemini لكل قطعة تُستخدم فقط كأوزان
    نسبية بين قطع نفس المشهد — يُعاد تحجيمها هنا تلقائياً لتطابق تماماً
    المدة الحقيقية المُقاسة من الصوت الفعلي (scene_timings)، فلا يوجد أي
    احتمال انزياح عن توقيت الكلام الحقيقي مهما أخطأ تقدير Gemini للثواني.
    """
    units = []
    for entry, timing in zip(scene_entries, scene_timings):
        visuals = entry["visuals"]
        weights = [max(0.3, float(v.get("duration_seconds", 1.0) or 1.0)) for v in visuals]
        total_weight = sum(weights)
        total_frames = timing["duration_frames"]

        cursor = timing["start_frame"]
        remaining_frames = total_frames
        for idx, (visual, weight) in enumerate(zip(visuals, weights)):
            is_last = idx == len(visuals) - 1
            if is_last:
                frames = remaining_frames  # آخر قطعة تاخذ الباقي بالضبط (يمنع انزياح تقريب)
            else:
                frames = max(1, round(total_frames * weight / total_weight))
                frames = min(frames, max(1, remaining_frames - (len(visuals) - idx - 1)))
            units.append({
                "narration": entry["narration"],
                "keywords": visual.get("keywords") or ["abstract background"],
                "start_frame": cursor,
                "duration_frames": max(1, frames),
            })
            cursor += frames
            remaining_frames -= frames
    return units


def _resolve_visual_units(visual_units: list[dict], workdir: str, file_prefix: str,
                           is_short: bool, topic_context: str, verify: bool,
                           sleep_between_seconds: float) -> list[dict]:
    """
    يعالج كل وحدة بصرية بالترتيب الصارم — لا ننتقل للوحدة التالية إلا بعد
    حسم مصير الحالية نهائياً (نجاح أو استنفاد كل الاحتمالات)، حسب الطلب:

      لكل وحدة بصرية:
        1) جلب 3 مرشحين (target_count=3) وتجربتهم بالترتيب: تحميل فعلي ثم
           تحقق تطابق (Gemini→Groq→CLIP عبر media_relevance_checker.py).
           أول مرشح ينجح = يُعتمد فوراً وننتقل للوحدة التالية.
        2) لو رُفض/فشل تحميل الثلاثة، ولسا عندنا محاولات استبدال متبقية
           (MAX_GEMINI_REPLACEMENTS): نرجع لـ Gemini ونقول له "هذي الكلمات
           لم أجد بها شيء، استبدلها لي لنفس الجملة"، ونكرر الخطوة 1 بالكلمات
           الجديدة الراجعة منه.
        3) بعد استنفاد كل محاولات الاستبدال: نلجأ لاحتياط عام حي (بحث فعلي
           بكلمات عامة عبر asset_fetcher، وليس رابطاً ثابتاً قد يموت لاحقاً)،
           أو لو فشل حتى هذا، نمدّد مدة الوحدة السابقة بدل ترك فجوة سوداء.
    """
    media_items = []
    target_count = 3 if verify else 1

    for i, unit in enumerate(visual_units):
        narration = unit["narration"]
        current_keywords = list(unit["keywords"])
        tried_keywords: list[str] = []
        local_path, media_type = None, "image"
        replacement_round = 0

        while True:
            prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
            media_list = asset_fetcher.get_media_for_scene(
                current_keywords, target_count=target_count, is_short=is_short,
                prefer_video=prefer_video, topic_context=topic_context,
            )
            tried_keywords.extend(current_keywords)

            for item in media_list:
                temp_type = item["type"]
                temp_path = f"{workdir}/{file_prefix}_{i}" + (".mp4" if temp_type == "video" else ".jpg")
                downloaded = (
                    asset_fetcher.download_video(item["url"], temp_path) if temp_type == "video"
                    else asset_fetcher.download_image(item["url"], temp_path)
                )
                if not downloaded:
                    continue

                if verify:
                    if media_relevance_checker.verify_media_file(temp_path, narration):
                        local_path, media_type = temp_path, temp_type
                        break
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                else:
                    local_path, media_type = temp_path, temp_type
                    break

            if local_path:
                break  # نجحت هذي الوحدة — الانتقال للوحدة التالية فقط الآن

            if not verify or replacement_round >= MAX_GEMINI_REPLACEMENTS:
                break  # لا فحص أصلاً (verify=False) أو استنفدنا محاولات الاستبدال

            replacement_round += 1
            print(f"[MONTAGE] الوحدة {i}: رُفض/فشل كل مرشحي {current_keywords}. "
                  f"طلب كلمات بديلة من Gemini (استبدال {replacement_round}/{MAX_GEMINI_REPLACEMENTS})...")
            new_keywords = script_writer.suggest_replacement_visual(narration, tried_keywords)
            if not new_keywords:
                print("[MONTAGE WARNING] لم يُرجع Gemini كلمات بديلة صالحة. التوقف عن محاولة هذي الوحدة.")
                break
            current_keywords = new_keywords

        if not local_path:
            print(f"[MONTAGE WARNING] فشلت كل محاولات الوحدة {i} (بما فيها استبدال Gemini). "
                  f"جاري تجربة احتياط عام حي...")
            fallback_list = asset_fetcher.get_media_for_scene(
                GENERIC_LAST_RESORT_KEYWORDS, target_count=1, is_short=is_short,
                prefer_video=False, topic_context="",
            )
            if fallback_list:
                temp_path = f"{workdir}/{file_prefix}_{i}_fallback.jpg"
                if asset_fetcher.download_image(fallback_list[0]["url"], temp_path):
                    local_path, media_type = temp_path, "image"

        if not local_path:
            print(f"[MONTAGE ERROR] فشل تام في توفير وسائط الوحدة {i} (حتى الاحتياط العام فشل).")
            if media_items:
                print("[MONTAGE] سيتم تمديد مدة الوحدة السابقة بدلاً من هذي الوحدة.")
                media_items[-1]["durationFrames"] += unit["duration_frames"]
            continue

        media_items.append({
            "type": media_type,
            "localPath": local_path,
            "startFrame": unit["start_frame"],
            "durationFrames": unit["duration_frames"],
        })

        if sleep_between_seconds:
            time.sleep(sleep_between_seconds)

    return media_items


def build_short_montage(short_script: dict, narration_text: str, topic: str, workdir: str) -> dict:
    """
    مونتاج فيديو الشورت الكامل. يرجع dict:
    {"video_path": str|None, "audio_path": str, "duration_seconds": float,
     "media_items": list[dict]}
    video_path يكون None لو فشل تحميل كل الوسائط (نفس سلوك الأصل: يتوقف
    الاستدعاء الأعلى وينبّه بدل الرندرة على فيديو فارغ).

    ملاحظة: أصبحت الدالة تبني كل شيء داخلياً من short_script مباشرة (بدل
    استقبال scene_narrations/scene_keywords جاهزة من shorts_pipeline.py)،
    لأن كل مشهد الآن قد يتحول لعدة "وحدات بصرية" (visual units) بمدد
    مختلفة — راجع _build_scene_entries و_expand_scene_to_visual_units.
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

    scene_entries = _build_scene_entries(short_script)
    scene_narrations = [e["narration"] for e in scene_entries]
    scene_timings = voice_and_captions.map_scenes_to_timing(
        scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=total_frames
    )
    visual_units = _expand_scene_to_visual_units(scene_entries, scene_timings)

    # Pixabay API يفشل بخطأ 400 لو كان الاستعلام طويلاً جداً؛ نأخذ فقط أول
    # 3-4 كلمات من الموضوع لإعطاء سياق كافٍ بدون كسر الـ API
    short_topic_context = " ".join(topic.split()[:4]) if topic else ""

    media_items = _resolve_visual_units(
        visual_units, workdir, file_prefix="short_scene", is_short=True,
        topic_context=short_topic_context, verify=True, sleep_between_seconds=4,
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
    بصري لكل وحدة (verify=False، لم يكن مفعّلاً بالنسخة الأصلية لهذا المسار
    تحديداً، ولذا لا يوجد استبدال عبر Gemini هنا أيضاً — يُستخدم أول مرشح
    مباشرة كالسابق). الفرق المكتسب تلقائياً هنا: كل مشهد قد يتحول لعدة
    وحدات بصرية بمدد مختلفة بدل صورة ثابتة واحدة تغطي كل مدته (نفس إصلاح
    مسار الشورت)، وتوزيع جلب الوسائط بين Pixabay و Pexels.
    """
    audio_path = f"{workdir}/long_audio.mp3"
    captions_path = f"{workdir}/long_captions.json"
    voice_and_captions.generate_voice_and_captions(narration_text, audio_path, captions_path)

    with open(captions_path, "r", encoding="utf-8") as f:
        word_events = json.load(f)

    scene_entries = _build_scene_entries(long_script)
    scene_narrations = [e["narration"] for e in scene_entries]
    total_frames = config.LONG_VIDEO_TARGET_SECONDS * config.VIDEO_FPS
    scene_timings = voice_and_captions.map_scenes_to_timing(
        scene_narrations, word_events, fps=config.VIDEO_FPS, total_frames=total_frames
    )
    visual_units = _expand_scene_to_visual_units(scene_entries, scene_timings)

    media_items = _resolve_visual_units(
        visual_units, workdir, file_prefix="scene", is_short=False, topic_context="",
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
