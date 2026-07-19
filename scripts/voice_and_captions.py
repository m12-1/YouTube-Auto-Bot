"""
voice_and_captions.py
يستخدم edge-tts (مجاني بالكامل) لتحويل السرد لصوت، ويلتقط أحداث WordBoundary
لإنتاج ملف JSON بتوقيت كل كلمة بالميلي ثانية — هذا ما يُغذّي الكابشن المتزامن
كلمة-بكلمة داخل Remotion.
"""
import asyncio
import json
import edge_tts

VOICE = "en-US-GuyNeural"  # صوت أمريكي واضح، يمكن تغييره لاحقاً حسب الأداء


async def _generate(text: str, audio_out_path: str, captions_out_path: str):
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

    with open(captions_out_path, "w", encoding="utf-8") as f:
        json.dump(word_events, f, ensure_ascii=False, indent=2)


def generate_voice_and_captions(text: str, audio_out_path: str, captions_out_path: str):
    asyncio.run(_generate(text, audio_out_path, captions_out_path))
    return audio_out_path, captions_out_path


def map_scenes_to_timing(scene_narrations: list[str], word_events: list[dict],
                          fps: int, total_frames: int) -> list[dict]:
    """
    تحل مشكلة أساسية بالنسخة السابقة: الصور كانت تتبدّل كل X ثانية ثابتة
    بدون أي علاقة بالمشهد اللي يتكلم عنه الراوي فعلياً. هذي الدالة تربط كل
    مشهد بزمنه الحقيقي من أحداث WordBoundary (نفس الترتيب اللي انبنى منه
    النص المُرسل لـ edge-tts، لذا الكلمات تتطابق بالتسلسل).

    ملاحظة دقة: الفصل بـ split() تقريبي (فاصلة/نقطة قد تلتصق بكلمة)، فقد
    يصير انزياح بسيط (أجزاء من الثانية) مع تراكم المشاهد — مقبول لتوقيت
    تبديل مشاهد بصرية، وليس حرج مثل توقيت الكابشن كلمة-بكلمة نفسه.

    يرجع قائمة [{"start_frame": int, "duration_frames": int}, ...] بنفس
    عدد وترتيب scene_narrations.
    """
    timings = []
    word_ptr = 0
    n_words_total = len(word_events)

    for i, narration in enumerate(scene_narrations):
        wc = max(1, len(narration.split()))
        start_idx = min(word_ptr, max(0, n_words_total - 1))
        end_idx = min(word_ptr + wc - 1, max(0, n_words_total - 1))

        if n_words_total == 0:
            start_ms, end_ms = 0, 0
        else:
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
