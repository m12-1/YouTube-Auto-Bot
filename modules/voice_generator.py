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


def compute_scene_timings(scenes: list[dict], word_boundaries: list[dict]) -> dict[str, tuple[float, float]]:
    """
    يحسب زمن بداية/نهاية كل مشهد فعليًا من الصوت المولَّد، بمطابقة نص كل
    scene["text"] مع تسلسل الكلمات المنطوقة (word_boundaries)، بدل الاعتماد
    فقط على best_segment الخام من مرحلة التحقق البصري (المرحلة 5) والذي لا
    علاقة له بزمن نطق النص الفعلي. هذا يحل مشكلة انزياح الصوت عن الصورة.

    يفترض أن المشاهد مرتّبة بنفس ترتيب النص الأصلي (narration) وأنها تغطيه
    تباعًا دون تداخل، وهو ما يبنيه script_and_seo_planner.
    """
    if not word_boundaries:
        return {}

    flat_words = [w["text"].strip() for w in word_boundaries]
    cursor = 0
    timings: dict[str, tuple[float, float]] = {}

    for scene in scenes:
        scene_words = [w for w in scene["text"].split() if w]
        n = len(scene_words)
        if n == 0 or cursor >= len(flat_words):
            continue
        end_idx = min(cursor + n, len(flat_words))
        start_wb = word_boundaries[cursor]
        end_wb = word_boundaries[end_idx - 1]
        start_time = start_wb["offset_seconds"]
        end_time = end_wb["offset_seconds"] + end_wb["duration_seconds"]
        timings[scene["id"]] = (start_time, end_time)
        cursor = end_idx

    return timings


if __name__ == "__main__":
    bounds = generate_voice("مرحبًا، هذا اختبار توليد الصوت.", "/tmp/test_voice.mp3")
    print(bounds)
