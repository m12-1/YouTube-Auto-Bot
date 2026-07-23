"""
parallel_scene_fetcher.py
ملف مستقل يُستدعى عند الحاجة فقط (من video_montage.py) — مهمته الوحيدة:
حل كل "الوحدات البصرية" (visual units) لفيديو واحد بالتوازي، بدل معالجتها
واحدة تلو الأخرى.

═══════════════════ التحديث الجذري: اختيار أفضل مرشح ═══════════════════
بدل قبول أول مرشح يتجاوز عتبة 7/10 والتوقف فوراً (ما كان يسمح بمرور
مشاهد "مقبولة بالكاد" رغم وجود مرشحين أفضل بكثير)، الآن:

  1. لكل وحدة بصرية، نجلب عدة مرشحين (حتى 3) من كل مزوّد.
  2. نُقيّم كل مرشح عبر analysis_engine (Gemini/Groq → Puter → CLIP).
  3. نختار المرشح الأعلى درجة فقط.
  4. لو لم يتجاوز أي مرشح عتبة 7/10 ولسا عندنا محاولات استبدال:
     - نطلب كلمات بديلة من Gemini (suggest_replacement_visual).
     - نعيد البحث بالكلمات الجديدة عبر كل المصادر.
  5. لو انتهت كل المحاولات ولا يوجد مرشح ≥ 7/10:
     - نقبل أفضل مرشح شوهد (الأعلى درجة) حتى لو كان 5/10 فقط.
     - لو لا يوجد أي مرشح ≥ 5/10 → نمدد المشهد السابق.

لا توجد كلمات احتياطية عامة ("cinematic background", "nature footage"...)
— أي مشهد يُعرض يجب أن يكون ناتجاً عن بحث مرتبط بالنص السردي فعلاً.
"""
import concurrent.futures
import os
import random

from scripts import asset_fetcher, media_relevance_checker, script_writer

# كم مرة نطلب من Gemini كلمات بديلة لوحدة بصرية فشلت مع كل المزودين
MAX_GEMINI_REPLACEMENTS = 2

# عتبة القبول المثالي (أول مرشح يتجاوزها يُعتمد فوراً بلا انتظار البقية)
IDEAL_THRESHOLD = 7.0

# عتبة القبول الأدنى (لو لم نجد مرشحاً ≥ IDEAL_THRESHOLD بعد كل المحاولات،
# نقبل أفضل مرشح شوهد طالما درجته ≥ هذه العتبة — أفضل من تمديد المشهد السابق)
MINIMUM_ACCEPTABLE_SCORE = 5.0


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
    يحل مصير وحدة بصرية واحدة بالكامل:
    - يبحث عبر كل المزودين المتاحين.
    - يُقيّم كل مرشح ويحتفظ بأفضلهم (الأعلى درجة).
    - لو وجد مرشحاً ≥ IDEAL_THRESHOLD (7/10) يتوقف فوراً.
    - لو لم يجد، يطلب كلمات بديلة من Gemini ويعيد البحث.
    - في النهاية: يقبل أفضل مرشح شوهد ≥ MINIMUM_ACCEPTABLE_SCORE (5/10).
    - لو لا يوجد أي مرشح مقبول → يرجع بلا مسار (يُمدد المشهد السابق).

    يُشغَّل هذا داخل خيط منفصل (thread) لكل وحدة — التوازي الفعلي بين
    المشاهد يحدث لأن عدة استدعاءات لهذه الدالة تعمل بنفس الوقت.
    """
    narration = unit["narration"]
    current_keywords = list(unit["keywords"])
    tried_urls: set = set()
    replacement_round = 0

    # أفضل مرشح شوهد حتى الآن عبر كل الجولات (حتى لو لم يتجاوز IDEAL_THRESHOLD)
    best_seen: dict | None = None  # {"path": str, "type": str, "score": float}

    while True:
        found_ideal = False

        for provider in providers:
            prefer_video = random.random() < asset_fetcher.VIDEO_PREFERENCE_RATIO
            media_list = asset_fetcher.get_media_for_scene(
                current_keywords, target_count=3, is_short=is_short,
                prefer_video=prefer_video, topic_context=topic_context,
                providers=[provider], exclude_urls=tried_urls,
            )
            for item in media_list:
                if item["url"] in tried_urls:
                    continue
                tried_urls.add(item["url"])
                temp_type = item["type"]
                temp_path = f"{workdir}/{file_prefix}_{idx}_cand{len(tried_urls)}" + (".mp4" if temp_type == "video" else ".jpg")
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
                    result = media_relevance_checker.verify_media_file(temp_path, narration, topic_context)
                    score = result.get("score", 0.0) if isinstance(result, dict) else (10.0 if result else 0.0)
                    passed = result.get("passed", False) if isinstance(result, dict) else bool(result)

                    print(f"[PARALLEL FETCH] الوحدة {idx}: مرشح من {provider} حصل على {score:.1f}/10")

                    # تتبع أفضل مرشح شوهد
                    if best_seen is None or score > best_seen["score"]:
                        # حذف الملف القديم لو كان موجوداً
                        if best_seen and best_seen["path"] != temp_path:
                            try:
                                os.remove(best_seen["path"])
                            except Exception:
                                pass
                        best_seen = {"path": temp_path, "type": temp_type, "score": score}
                    elif temp_path != (best_seen["path"] if best_seen else None):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    # لو تجاوز العتبة المثالية → اعتمده فوراً بلا انتظار البقية
                    if passed and score >= IDEAL_THRESHOLD:
                        found_ideal = True
                        break
                else:
                    # بلا فحص (verify=False): أول تنزيل ناجح يُعتمد
                    best_seen = {"path": temp_path, "type": temp_type, "score": 10.0}
                    found_ideal = True
                    break

            if found_ideal:
                break  # وجدنا مرشحاً مثالياً — لا داعي لتجربة بقية المزودين

        if found_ideal:
            break  # اعتماد فوري

        # جُرِّب كل المزودين المتاحين هذه الجولة بدون مرشح مثالي
        if not verify or replacement_round >= MAX_GEMINI_REPLACEMENTS:
            break

        replacement_round += 1
        print(f"[PARALLEL FETCH] الوحدة {idx}: لم يُعثر على مرشح مثالي (≥{IDEAL_THRESHOLD}/10) "
              f"لكلمات {current_keywords}. "
              f"طلب كلمات بديلة من Gemini (استبدال {replacement_round}/{MAX_GEMINI_REPLACEMENTS})...")
        new_keywords = script_writer.suggest_replacement_visual(narration, list(current_keywords))
        if not new_keywords:
            print(f"[PARALLEL FETCH] الوحدة {idx}: لم يُرجع Gemini كلمات بديلة صالحة. التوقف.")
            break
        current_keywords = new_keywords
        # الجولة الجديدة تبدأ من نفس ترتيب المزودين لكن
        # exclude_urls (tried_urls) يضمن أنه يجلب وسيطاً مختلفاً هذه المرة.

    # ─── قرار نهائي ───
    local_path, media_type = None, "image"
    if best_seen:
        if best_seen["score"] >= MINIMUM_ACCEPTABLE_SCORE:
            local_path = best_seen["path"]
            media_type = best_seen["type"]
            if best_seen["score"] < IDEAL_THRESHOLD:
                print(f"[PARALLEL FETCH] الوحدة {idx}: لم يُعثر على مرشح مثالي (≥{IDEAL_THRESHOLD}/10). "
                      f"قبول أفضل مرشح شوهد (درجته {best_seen['score']:.1f}/10) — "
                      f"أفضل من تمديد المشهد السابق.")
            else:
                print(f"[PARALLEL FETCH] الوحدة {idx}: تم اعتماد مرشح مثالي (درجته {best_seen['score']:.1f}/10).")
        else:
            print(f"[PARALLEL FETCH] الوحدة {idx}: أفضل مرشح شوهد درجته {best_seen['score']:.1f}/10 "
                  f"(أقل من الحد الأدنى {MINIMUM_ACCEPTABLE_SCORE}/10). سيتم تمديد المشهد السابق.")
            try:
                os.remove(best_seen["path"])
            except Exception:
                pass
    else:
        print(f"[PARALLEL FETCH] الوحدة {idx}: لم يُعثر على أي مرشح. سيتم تمديد المشهد السابق.")

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
