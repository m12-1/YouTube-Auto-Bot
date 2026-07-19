"""
voice_and_captions.py
يستخدم edge-tts (مجاني بالكامل) لتحويل السرد لصوت، ويلتقط أحداث WordBoundary
لإنتاج ملف JSON بتوقيت كل كلمة بالميلي ثانية — هذا ما يُغذّي الكابشن المتزامن
كلمة-بكلمة داخل Remotion.
"""
import asyncio
import json
import edge_tts
import re

VOICE = "en-US-GuyNeural"  # صوت أمريكي واضح، يمكن تغييره لاحقاً حسب الأداء


def _build_synthetic_word_events(text: str, total_duration_ms: float = 55_000) -> list[dict]:
    """
    Fallback: عندما لا تصل أحداث WordBoundary من edge-tts (مشكلة شائعة فيبيئات
    CI مثل GitHub Actions حيث يُحجب WebSocket أحياناً)، نولد توقيتاً اصطناعياً
    بتقسيم المدة الكلية بالتساوي على عدد الكلمات الفعليين.

    هذا يضمن أن الكابشن يظهر والمشاهد تتوزع بشكل صحيح بدل أن يكون
    الفيديو بصورة واحدة فقط (كان يحدث لأن كل المشاهد تحصل على start_frame=0).
    """
    words = text.split()
    if not words:
        return []
        
    # حساب الأوزان الزمنية لكل كلمة: الكلمات الطويلة تستغرق وقتاً أطول، 
    # وعلامات الترقيم تعني وجود "وقفة" (صمت) بعدها في الصوت الحقيقي.
    weights = []
    for word in words:
        clean_word = re.sub(r'[^\w]', '', word)
        char_count = max(1, len(clean_word))
        
        weight = char_count * 1.0
        
        if re.search(r'[.!?]+$', word):
            weight += 8.0  # وقفة طويلة نهاية الجملة
        elif re.search(r'[,;:]+$', word):
            weight += 4.0  # وقفة قصيرة في المنتصف
            
        weights.append(weight)

    total_weight = sum(weights)
    ms_per_weight_unit = total_duration_ms / total_weight
    
    events = []
    current_ms = 0.0
    for word, weight in zip(words, weights):
        word_duration = weight * ms_per_weight_unit
        events.append({
            "word": word,
            "start_ms": round(current_ms, 2),
            "duration_ms": round(word_duration * 0.95, 2),
        })
        current_ms += word_duration
        
    return events


async def _generate(text: str, audio_out_path: str, captions_out_path: str,
                    target_duration_ms: float = 55_000):
    communicate = edge_tts.Communicate(text, VOICE)
    word_events = []

    with open(audio_out_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_events.append({
                    "word": chunk["text"],
                    "start_ms": chunk["offset"] / 10000,   # يحوّل من 100-nanosecond units لـ ms
                    "duration_ms": chunk["duration"] / 10000,
                })

    # ← إصلاح رئيسي: قياس مدة الصوت الفعلي لاستخدامها في حساب التوقيت الاصطناعي بدقة
    try:
        from mutagen.mp3 import MP3
        audio_info = MP3(audio_out_path)
        actual_duration_ms = audio_info.info.length * 1000
    except Exception as e:
        print(f"[WARNING] فشل حساب المدة الفعلية للصوت في voice_and_captions: {e}")
        actual_duration_ms = target_duration_ms

    if not word_events:
        print(f"[WARNING] edge-tts لم يُنتج WordBoundary events. "
              f"سيُستخدم توقيت اصطناعي متساوٍ لـ {len(text.split())} كلمة على مدة {actual_duration_ms/1000:.1f} ثانية.")
        word_events = _build_synthetic_word_events(text, actual_duration_ms)

    with open(captions_out_path, "w", encoding="utf-8") as f:
        json.dump(word_events, f, ensure_ascii=False, indent=2)


def generate_voice_and_captions(text: str, audio_out_path: str, captions_out_path: str,
                                 target_duration_ms: float = 55_000):
    asyncio.run(_generate(text, audio_out_path, captions_out_path, target_duration_ms))
    return audio_out_path, captions_out_path


def _normalize_word(w: str) -> str:
    """يزيل علامات الترقيم ويحوّل لحروف صغيرة، لمقارنة الكلمات بتجاهل
    الاختلافات السطحية بين تقطيع edge-tts وتقطيع split() النصي."""
    return re.sub(r'[^\w]', '', w).lower()


def map_scenes_to_timing(scene_narrations: list[str], word_events: list[dict],
                          fps: int, total_frames: int) -> list[dict]:
    """
    تحل مشكلة أساسية بالنسخة السابقة: الصور كانت تتبدّل كل X ثانية ثابتة
    بدون أي علاقة بالمشهد اللي يتكلم عنه الراوي فعلياً. هذي الدالة تربط كل
    مشهد بزمنه الحقيقي من أحداث WordBoundary.

    ← إصلاح انزياح التزامن التراكمي: النسخة السابقة كانت تتقدّم بمؤشر
    الكلمات بعدد ثابت (len(narration.split())) بدون التحقق أن هذا يطابق
    فعلياً عدد كلمات WordBoundary لنفس المقطع. edge-tts لا يقسّم الكلمات
    بنفس طريقة split() دائماً (اختصارات، أرقام، علامات ترقيم ملتصقة)،
    فأي فرق ولو بكلمة واحدة في مشهد يزيح كل المشاهد اللي بعده — وهذا
    يفسّر الملاحظة: يبدأ متزامن، ينزاح تدريجياً، وأحياناً يعود يتصادف
    بسبب تراكم أخطاء متعاكسة.

    الحل: نطابق فعلياً كلمات كل مشهد (منظّفة من الترقيم) مع كلمات
    word_events بدءاً من موقع المؤشر الحالي، بدل الافتراض الأعمى بعدد
    الكلمات. لو حصل عدم تطابق نبحث ضمن نافذة صغيرة حول الموقع المتوقع
    لنعيد محاذاة المؤشر بدل ما نخليه ينجرف إلى الأبد.

    يرجع قائمة [{"start_frame": int, "duration_frames": int}, ...] بنفس
    عدد وترتيب scene_narrations.
    """
    timings = []
    word_ptr = 0
    n_words_total = len(word_events)

    if n_words_total == 0:
        # Uniform distribution fallback
        count = len(scene_narrations)
        frames_per_scene = total_frames // count
        return [{"start_frame": i * frames_per_scene, "duration_frames": frames_per_scene} for i in range(count)]

    normalized_events = [_normalize_word(e["word"]) for e in word_events]
    REALIGN_WINDOW = 6  # نافذة البحث لإعادة المحاذاة عند اكتشاف انزياح

    for i, narration in enumerate(scene_narrations):
        scene_words = [_normalize_word(w) for w in narration.split() if _normalize_word(w)]
        wc = max(1, len(scene_words))

        # إعادة محاذاة: لو أول كلمة بالمشهد لا تطابق ما هو متوقع عند
        # word_ptr، نبحث ضمن نافذة قريبة عن أول تطابق حقيقي بدل الاستمرار
        # بمؤشر منزاح طوال بقية الفيديو
        if scene_words and word_ptr < n_words_total:
            target = scene_words[0]
            if normalized_events[word_ptr] != target:
                search_start = max(0, word_ptr - REALIGN_WINDOW)
                search_end = min(n_words_total, word_ptr + REALIGN_WINDOW + 1)
                for candidate in range(search_start, search_end):
                    if normalized_events[candidate] == target:
                        word_ptr = candidate
                        break
                # لو ما لقينا تطابق بالنافذة، نكمل بنفس word_ptr الحالي
                # (أفضل تخمين متاح) بدل تجميد التنفيذ

        start_idx = min(word_ptr, max(0, n_words_total - 1))
        end_idx = min(word_ptr + wc - 1, max(0, n_words_total - 1))

        start_ms = word_events[start_idx]["start_ms"]
        end_ms = word_events[end_idx]["start_ms"] + word_events[end_idx]["duration_ms"]

        start_frame = round((start_ms / 1000) * fps)
        end_frame = round((end_ms / 1000) * fps)
        word_ptr += wc
        timings.append({"start_frame": start_frame, "end_frame": end_frame})

    # نضمن التسلسل الصحيح: كل مشهد يبدأ حيث انتهى اللي قبله بالضبط (بدون
    # فجوة سوداء أو تراكب)، وآخر مشهد يمتد لنهاية الفيديو فعلياً
    for i in range(len(timings) - 1):
        timings[i]["end_frame"] = timings[i + 1]["start_frame"]
    if timings:
        timings[0]["start_frame"] = 0
        timings[-1]["end_frame"] = max(timings[-1]["end_frame"], total_frames)

    result = []
    for t in timings:
        duration = max(1, t["end_frame"] - t["start_frame"])
        result.append({"start_frame": t["start_frame"], "duration_frames": duration})
    return result


if __name__ == "__main__":
    import sys
    generate_voice_and_captions(
        sys.argv[1] if len(sys.argv) > 1 else "This is a test narration.",
        "output_audio.mp3",
        "output_captions.json",
    )
