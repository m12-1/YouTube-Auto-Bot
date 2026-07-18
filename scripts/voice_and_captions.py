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


if __name__ == "__main__":
    import sys
    generate_voice_and_captions(
        sys.argv[1] if len(sys.argv) > 1 else "This is a test narration.",
        "output_audio.mp3",
        "output_captions.json",
    )
