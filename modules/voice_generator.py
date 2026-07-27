"""
توليد الصوت من نص narration باستخدام Edge-TTS، مع الحصول على word-boundary
timing الفعلي (لإصلاح مشكلة "no word-boundary timing" الظاهرة سابقًا في اللوج)
لاستخدامه لاحقًا في تزامن الترجمة.
"""

import asyncio
import logging

import edge_tts

logger = logging.getLogger("modules.voice_generator")

DEFAULT_VOICE = "ar-SA-HamedNeural"  # صوت عربي واضح؛ يمكن تبديله حسب التفضيل


async def _generate(text: str, out_path: str, voice: str):
    communicate = edge_tts.Communicate(text, voice)
    word_boundaries = []
    with open(out_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_boundaries.append({
                    "text": chunk["text"],
                    "offset_seconds": chunk["offset"] / 10_000_000,  # 100-ns units -> seconds
                    "duration_seconds": chunk["duration"] / 10_000_000,
                })
    return word_boundaries


def generate_voice(text: str, out_path: str, voice: str = DEFAULT_VOICE) -> list[dict]:
    """يولّد ملف الصوت ويعيد قائمة word boundaries (نص + توقيت) لاستخدامها في الترجمة."""
    boundaries = asyncio.run(_generate(text, out_path, voice))
    if not boundaries:
        logger.warning("لم يتم استلام أي word boundaries من Edge-TTS لهذا النص.")
    return boundaries


if __name__ == "__main__":
    bounds = generate_voice("مرحبًا، هذا اختبار توليد الصوت.", "/tmp/test_voice.mp3")
    print(bounds)
