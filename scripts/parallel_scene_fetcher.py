"""
parallel_scene_fetcher.py
ملف مستقل يُستدعى عند الحاجة فقط (من video_montage.py) — مهمته الوحيدة:
حل كل "الوحدات البصرية" (visual units) لفيديو واحد بالتوازي، بدل معالجتها
واحدة تلو الأخرى.

الفكرة (حسب الطلب):
-------------------
لو عندنا مثلاً 10 مشاهد ومزوّدين متاحين (Pixabay, Pexels)، نبحث عن أكبر
عدد ممكن من المشاهد بنفس الوقت — كل "عامل" (worker) يمسك مشهداً لم يُحسم
بعد، وبمجرد ما يجلب له وسيطاً (ينجح أو يستنفد محاولاته) يرجع فوراً ليمسك
مشهداً آخر لم يُحسم بعد، بدل انتظار كل المشاهد الأخرى.

منطق الرفض والتبديل بين المزودين لنفس المشهد:
----------------------------------------------
لكل مشهد على حدة: نجرب مزوّداً، فلو رجع وسيطاً ورفضه التحليل، نجرب المزوّد
التالي (وليس نفس المزوّد) لنفس الكلمات. لو جربنا كل المزودين المتاحين مرة
واحدة بدون نجاح، نرجع للمزوّد الأول من جديد — لكن نطلب منه "وسيطاً آخر
غير الذي جلبه أول مرة" (عبر استبعاد الروابط المرفوضة سابقاً)، وليس نفس
الوسيط المرفوض بالضبط.

هذا الملف لا يستدعي نفسه تلقائياً بأي مكان — يُستورد ويُستدعى فقط عند
بناء المونتاج (video_montage.py)، تماشياً مع طلب فصل "عملية البحث وجلب
الفيديوهات" في ملف مستقل يُستدعى عند الحاجة.
"""
import concurrent.futures
import os
import random
import time

from scripts import asset_fetcher, analysis_engine, media_relevance_checker, script_writer

# كم مرة نطلب من Gemini كلمات بديلة لوحدة بصرية فشلت مع كل المزودين
MAX_GEMINI_REPLACEMENTS = 2

GENERIC_LAST_RESORT_KEYWORDS = ["cinematic background", "abstract technology", "nature footage"]


def _check_resolution(file_path: str, media_type: str) -> bool:
    """نفس فحص الدقة الموجود بـ video_montage.py (مُكرّر هنا محلياً لتجنّب
    استيراد دائري، فالفحص بسيط وخفيف)."""
    try:
        if media_type == "video":
            import subprocess
            cmd = [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", file_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and "x" in result.stdout.strip():
                w, h = (int(x) for x in result.stdout.strip().split("x"))
                if min(w, h) < 720:
                    return False
        else:
            from PIL import Image
            with Image.open(file_path) as img:
                w, h = img.size
                if min(w, h) < 720:
                    return False
    except Exception as e:
        print(f"[PARALLEL FETCH] تعذر فحص دقة {file_path}: {e}. سيتم قبوله.")
    return True


def _resolve_single_unit(unit: dict, idx: int, workdir: str, file_prefix: str,
                          is_short: bool, topic_context: str, verify: bool,
                          providers: list[str]) -> dict:
    """
    يحل مصير وحدة بصرية واحدة بالكامل: يدور على المزودين المتاحين واحداً
    تلو الآخر (غير الذي جلب منه المرشح المرفوض أول مرة)، وباستبعاد أي
    رابط جُرِّب ورُفض سابقاً، حتى ينجح أحدهم أو تُستنفد كل المحاولات
    (بما فيها طلب كلمات بديلة من Gemini، ثم احتياط عام أخير).

    يُشغَّل هذا داخل خيط منفصل (thread) لكل وحدة — التوازي الفعلي بين
    المشاهد يحدث لأن عدة استدعاءات لهذه الدالة تعمل بنفس الوقت.
    """
    narration = unit["narration"]
    current_keywords = list(unit["keywords"])
    tried_urls: set = set()
    local_path, media_type = None, "image"
    replacement_round = 0

    while True:
        found_this_round = False
        # نجرب كل مزوّد على حدة لنفس الوحدة — أول من ينجح يُعتمد فوراً،
        # ولو رُفض وسيطه ننتقل لمزوّد آخر (غير الذي جلب منه) بدل تكرار المحاولة معه.
        for provider in providers:
            prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
            media_list = asset_fetcher.get_media_for_scene(
                current_keywords, target_count=2, is_short=is_short,
                prefer_video=prefer_video, topic_context=topic_context,
                providers=[provider], exclude_urls=tried_urls,
            )
            for item in media_list:
                if item["url"] in tried_urls:
                    continue
                tried_urls.add(item["url"])
                temp_type = item["type"]
                temp_path = f"{workdir}/{file_prefix}_{idx}" + (".mp4" if temp_type == "video" else ".jpg")
                downloaded = (
                    asset_fetcher.download_video(item["url"], temp_path) if temp_type == "video"
                    else asset_fetcher.download_image(item["url"], temp_path)
                )
                if not downloaded:
                    continue
                if not _check_resolution(temp_path, temp_type):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    continue

                if verify:
                    if media_relevance_checker.verify_media_file(temp_path, narration):
                        local_path, media_type = temp_path, temp_type
                        found_this_round = True
                        break
                    print(f"[PARALLEL FETCH] الوحدة {idx}: رفض التحليل وسيطاً من {provider}. "
                          f"تجربة مزوّد آخر لنفس الكلمات...")
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                else:
                    local_path, media_type = temp_path, temp_type
                    found_this_round = True
                    break
            if found_this_round:
                break  # نجحت هذي الوحدة — لا داعي لتجربة بقية المزودين

        if local_path:
            break

        # جُرِّب كل المزودين المتاحين مرة واحدة لهذه الجولة بدون نجاح
        if not verify or replacement_round >= MAX_GEMINI_REPLACEMENTS:
            break

        replacement_round += 1
        print(f"[PARALLEL FETCH] الوحدة {idx}: رُفض/فشل كل المزودين لكلمات {current_keywords}. "
              f"طلب كلمات بديلة من Gemini (استبدال {replacement_round}/{MAX_GEMINI_REPLACEMENTS})...")
        new_keywords = script_writer.suggest_replacement_visual(narration, list(current_keywords))
        if not new_keywords:
            print(f"[PARALLEL FETCH] الوحدة {idx}: لم يُرجع Gemini كلمات بديلة صالحة. التوقف عن هذي الوحدة.")
            break
        current_keywords = new_keywords
        # الجولة الجديدة تبدأ من نفس ترتيب المزودين (المرجع للأول) لكن
        # exclude_urls (tried_urls) يضمن أنه يجلب وسيطاً مختلفاً هذه المرة.

    if not local_path:
        print(f"[PARALLEL FETCH] الوحدة {idx}: تجربة احتياط عام أخير (يُفحص تطابقه أيضاً)...")
        for provider in providers:
            fb_list = asset_fetcher.get_media_for_scene(
                GENERIC_LAST_RESORT_KEYWORDS, target_count=2, is_short=is_short,
                prefer_video=False, topic_context="", providers=[provider], exclude_urls=tried_urls,
            )
            for fb_item in fb_list:
                if fb_item["url"] in tried_urls:
                    continue
                tried_urls.add(fb_item["url"])
                fb_type = fb_item["type"]
                fb_path = f"{workdir}/{file_prefix}_{idx}_fallback" + (".mp4" if fb_type == "video" else ".jpg")
                fb_downloaded = (
                    asset_fetcher.download_video(fb_item["url"], fb_path) if fb_type == "video"
                    else asset_fetcher.download_image(fb_item["url"], fb_path)
                )
                if not fb_downloaded:
                    continue
                if (not verify) or media_relevance_checker.verify_media_file(fb_path, narration):
                    local_path, media_type = fb_path, fb_type
                    break
                try:
                    os.remove(fb_path)
                except Exception:
                    pass
            if local_path:
                break

    return {
        "idx": idx,
        "local_path": local_path,
        "media_type": media_type,
        "start_frame": unit["start_frame"],
        "duration_frames": unit["duration_frames"],
    }


def resolve_visual_units_parallel(visual_units: list[dict], workdir: str, file_prefix: str,
                                   is_short: bool, topic_context: str, verify: bool) -> list[dict]:
    """
    نُقطة الدخول الوحيدة لهذا الملف. تحل كل الوحدات البصرية بالتوازي (عدد
    الخيوط المتزامنة = عدد المزودين المتاحين، بحد أدنى 2)، بدل معالجتها
    بالتسلسل الصارم كما كان بـ video_montage._resolve_visual_units.

    ترجع قائمة media_items بنفس شكل الأصل (type/localPath/startFrame/
    durationFrames) بترتيب الوحدات الأصلي، مع تمديد مدة الوحدة السابقة
    تلقائياً لو فشلت وحدة معينة تماماً (بدل فجوة سوداء) — بعد تجميع كل
    النتائج المتوازية، لأن هذا القرار يعتمد على الترتيب النهائي.
    """
    providers = asset_fetcher._available_providers()
    if not providers:
        print("[PARALLEL FETCH ERROR] لا يوجد أي مزوّد وسائط متاح (لا Pixabay ولا Pexels).")
        return []

    # عدد العمال المتزامنين: على الأقل بعدد المزودين، حتى تُستغل كل مزودات
    # البحث بنفس الوقت لمشاهد مختلفة (وأكثر قليلاً للسماح بمعالجة عدة
    # مشاهد متوازية حتى مع مزوّد واحد فقط متاح).
    max_workers = max(len(providers) * 2, 3)

    results_by_idx: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_resolve_single_unit, unit, i, workdir, file_prefix, is_short,
                        topic_context, verify, providers): i
            for i, unit in enumerate(visual_units)
        }
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            results_by_idx[res["idx"]] = res

    # نُعيد التجميع بالترتيب الأصلي، ونمدّد مدة آخر وحدة ناجحة بدل أي وحدة
    # فشلت تماماً (نفس سلوك الأصل تجاه المشاهد المرفوضة كلياً).
    media_items: list[dict] = []
    for i in range(len(visual_units)):
        res = results_by_idx.get(i)
        if res is None or not res["local_path"]:
            print(f"[PARALLEL FETCH] فشل تام بتوفير وسائط الوحدة {i}.")
            if media_items:
                media_items[-1]["durationFrames"] += (res["duration_frames"] if res else visual_units[i]["duration_frames"])
            continue
        media_items.append({
            "type": res["media_type"],
            "localPath": res["local_path"],
            "startFrame": res["start_frame"],
            "durationFrames": res["duration_frames"],
        })

    return media_items
