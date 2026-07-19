"""
voice_and_captions.py
يستخدم edge-tts (مجاني بالكامل) لتحويل السرد لصوت، ويلتقط أحداث WordBoundary
لإنتاج ملف JSON بتوقيت كل كلمة بالميلي ثانية — هذا ما يُغذّي الكابشن المتزامن
كلمة-بكلمة داخل Remotion.
"""
import asyncio
import json
import edge_tts

VOICE = "en-US-GuyNeural" 

async def _generate(text: str, audio_out_path: str, captions_out_path: str):
    communicate = edge_tts.Communicate(text, VOICE)
    word_events = []
    words = text.split()

    with open(audio_out_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_events.append({
                    "word": chunk["text"],
                    "start_ms": chunk["offset"] / 10000,
                    "duration_ms": chunk["duration"] / 10000,
                })

    # خيار احتياطي: إذا فشل الـ WordBoundary (بسبب حجب الـ WebSockets في الـ CI)
    if not word_events and words:
        total_duration_ms = 55000  # تقدير لـ 55 ثانية للشورت
        ms_per_word = total_duration_ms / len(words)
        for i, word in enumerate(words):
            word_events.append({
                "word": word,
                "start_ms": i * ms_per_word,
                "duration_ms": ms_per_word,
            })

    with open(captions_out_path, "w", encoding="utf-8") as f:
        json.dump(word_events, f, ensure_ascii=False, indent=2)

def generate_voice_and_captions(text: str, audio_out_path: str, captions_out_path: str):
    asyncio.run(_generate(text, audio_out_path, captions_out_path))
    return audio_out_path, captions_out_path

def map_scenes_to_timing(scene_narrations: list[str], word_events: list[dict],
                          fps: int, total_frames: int) -> list[dict]:
    timings = []
    n_words_total = len(word_events)
    
    # إصلاح: توزيع متساوٍ للمشاهد في حال فشل التوقيت الصوتي
    if n_words_total == 0:
        scene_duration = total_frames // len(scene_narrations)
        for i in range(len(scene_narrations)):
            timings.append({
                "start_frame": i * scene_duration,
                "end_frame": (i + 1) * scene_duration
            })
    else:
        word_ptr = 0
        for i, narration in enumerate(scene_narrations):
            wc = max(1, len(narration.split()))
            start_idx = min(word_ptr, max(0, n_words_total - 1))
            end_idx = min(word_ptr + wc - 1, max(0, n_words_total - 1))

            start_ms = word_events[start_idx]["start_ms"]
            end_ms = word_events[end_idx]["start_ms"] + word_events[end_idx]["duration_ms"]

            start_frame = round((start_ms / 1000) * fps)
            end_frame = round((end_ms / 1000) * fps)
            word_ptr += wc
            timings.append({"start_frame": start_frame, "end_frame": end_frame})

    # ضبط التسلسل
    for i in range(len(timings) - 1):
        timings[i]["end_frame"] = timings[i + 1]["start_frame"]
    if timings:
        timings[-1]["end_frame"] = max(timings[-1]["end_frame"], total_frames)

    result = []
    for t in timings:
        duration = max(1, t["end_frame"] - t["start_frame"])
        result.append({"start_frame": t["start_frame"], "duration_frames": duration})
    return result
