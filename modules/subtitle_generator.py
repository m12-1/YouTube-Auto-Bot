"""
توليد الترجمة (subtitles) من word boundaries، مع تجميع الكلمات حسب علامات
الترقيم بدل عدد كلمات ثابت، لتفادي قطع الجملة نحويًا في منتصف الشاشة.
"""

import logging

logger = logging.getLogger("modules.subtitle_generator")

_PAUSE_PUNCTUATION = ["،", ".", "!", "؟", "؛", ",", "?"]
_MAX_CHARS_PER_LINE = 42
_MAX_LINE_SECONDS = 4.0


def build_subtitle_segments(word_boundaries: list[dict]) -> list[dict]:
    """
    يجمّع word boundaries إلى مقاطع ترجمة، بحيث ينتهي المقطع عند علامة ترقيم
    أو عند تجاوز الحد الأقصى للأحرف/الزمن، وليس عند عدد كلمات ثابت.
    """
    segments = []
    current_words = []
    current_start = None

    def flush():
        if not current_words:
            return
        text = "".join(w["text"] for w in current_words).strip()
        start = current_start
        end = current_words[-1]["offset_seconds"] + current_words[-1]["duration_seconds"]
        segments.append({"text": text, "start": start, "end": end})

    for wb in word_boundaries:
        if current_start is None:
            current_start = wb["offset_seconds"]
        current_words.append(wb)

        joined = "".join(w["text"] for w in current_words)
        duration_so_far = wb["offset_seconds"] + wb["duration_seconds"] - current_start
        ends_with_pause = any(wb["text"].strip().endswith(p) for p in _PAUSE_PUNCTUATION)

        if ends_with_pause or len(joined) >= _MAX_CHARS_PER_LINE or duration_so_far >= _MAX_LINE_SECONDS:
            flush()
            current_words = []
            current_start = None

    flush()
    return segments


def write_srt(segments: list[dict], out_path: str):
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(out_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n{seg['text']}\n\n")
