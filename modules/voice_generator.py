"""
توليد الصوت من نص narration باستخدام Edge-TTS، مع الحصول على word-boundary
timing الفعلي (لإصلاح مشكلة "no word-boundary timing" الظاهرة سابقًا في اللوج)
لاستخدامه لاحقًا في تزامن الترجمة.
"""

import asyncio
import logging

import edge_tts

logger = logging.getLogger("modules.voice_generator")

DEFAULT_VOICE = "en-US-ChristopherNeural"  # صوت إنجليزي واضح وحيوي؛ يمكن تبديله حسب التفضيل


async def _generate(text: str, out_path: str, voice: str):
    # ملاحظة: بدءًا من الإصدارات الحديثة لمكتبة edge-tts (>=7.2.3)، تغيّر
    # الافتراضي من "WordBoundary" إلى "SentenceBoundary"، لذا يجب تمرير
    # boundary="WordBoundary" صراحة، وإلا لن تصل أي WordBoundary إطلاقًا
    # (تصل SentenceBoundary فقط ولا تُطابق الشرط أدناه).
    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
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
        # هذا خطأ فعلي مو مجرد تحذير: بدون word boundaries، compute_scene_timings
        # سيفقد التزامن الحقيقي مع الصوت لكل المشاهد (يلجأ لتوزيع نسبي احتياطي
        # بدل السكوت التام، لكن هذا التوزيع أقل دقة من التوقيت الفعلي).
        logger.error(
            "لم يتم استلام أي word boundaries من Edge-TTS لهذا النص — "
            "سيُستخدم توزيع زمني تقريبي (نسبي) بدل التوقيت الفعلي المتزامن مع الصوت."
        )
    return boundaries


def _proportional_fallback_timings(
    scenes: list[dict], total_duration: float
) -> dict[str, tuple[float, float]]:
    """توزيع نسبي احتياطي لمدة الصوت الإجمالية على المشاهد حسب عدد كلمات كل
    مشهد، يُستخدم فقط لو edge-tts لم يرجّع أي WordBoundary إطلاقًا (حالة
    معروفة مع الأصوات العربية). أقل دقة من التوقيت الفعلي لكنه أفضل بكثير
    من فقدان التزامن بصمت تام (timings = {})."""
    word_counts = [len([w for w in scene["text"].split() if w]) for scene in scenes]
    total_words = sum(word_counts) or 1

    timings: dict[str, tuple[float, float]] = {}
    cursor_time = 0.0
    for scene, n_words in zip(scenes, word_counts):
        share = (n_words / total_words) * total_duration
        start_time = cursor_time
        end_time = cursor_time + share
        timings[scene["id"]] = (start_time, end_time)
        cursor_time = end_time

    return timings


def compute_scene_timings(
    scenes: list[dict],
    word_boundaries: list[dict],
    total_duration: float | None = None,
) -> dict[str, tuple[float, float]]:
    """
    يحسب زمن بداية/نهاية كل مشهد فعليًا من الصوت المولَّد، بمطابقة نص كل
    scene["text"] مع تسلسل الكلمات المنطوقة (word_boundaries)، بدل الاعتماد
    فقط على best_segment الخام من مرحلة التحقق البصري (المرحلة 5) والذي لا
    علاقة له بزمن نطق النص الفعلي. هذا يحل مشكلة انزياح الصوت عن الصورة.

    يفترض أن المشاهد مرتّبة بنفس ترتيب النص الأصلي (narration) وأنها تغطيه
    تباعًا دون تداخل، وهو ما يبنيه script_and_seo_planner.

    لو لم تصل أي word_boundaries من edge-tts (حالة معروفة مع الأصوات
    العربية)، لا نرجع {} بصمت: نسجّل خطأً واضحًا ونستخدم توزيعًا نسبيًا
    لمدة الصوت الإجمالية (total_duration) بدل فقدان التزامن كليًا.
    """
    if not word_boundaries:
        if total_duration and total_duration > 0:
            logger.error(
                "لا توجد word boundaries — تم استخدام توزيع زمني نسبي "
                "احتياطي (total_duration=%.2fs) بدل التوقيت الفعلي المتزامن مع الصوت.",
                total_duration,
            )
            return _proportional_fallback_timings(scenes, total_duration)
        logger.error(
            "لا توجد word boundaries ولا total_duration احتياطي — "
            "تعذّر حساب أي توقيت مشاهد؛ سيعتمد المونتاج على best_segment الخام فقط."
        )
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
