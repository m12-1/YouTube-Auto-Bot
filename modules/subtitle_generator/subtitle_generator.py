"""
modules/subtitle_generator/subtitle_generator.py

Generates a subtitle timeline from word-level timings produced by the
Voice Generator: word-by-word subtitle chunks (grouped by a
configurable max words per line) with an animated-highlight flag for
the "active" word within each chunk.

Deterministic implementation — no AI call required.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "word_timings": [ {"word": str, "start": float, "end": float}, ... ]
    }

output_json:
    {
        "status": "success" | "error",
        "module": "subtitle_generator",
        "data": {
            "run_id": str,
            "topic": str,
            "subtitle_timeline": [
                {
                    "line_id": int,
                    "text": str,
                    "start": float,
                    "end": float,
                    "words": [
                        {
                            "word": str, "start": float, "end": float,
                            "highlight_color": str
                        },
                        ...
                    ]
                },
                ...
            ]
        },
        "error": str | null
    }
"""

from __future__ import annotations

from typing import Any, Dict, List

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger

logger = get_logger(__name__)

MODULE_NAME = "subtitle_generator"


def _chunk_word_timings(
    word_timings: List[Dict[str, Any]], max_words_per_line: int
) -> List[List[Dict[str, Any]]]:
    """
    Group word timings into subtitle lines of at most `max_words_per_line`.

    Args:
        word_timings: Ordered list of word timing dicts.
        max_words_per_line: Maximum words allowed per subtitle line.

    Returns:
        A list of word-timing chunks, one per subtitle line.
    """
    chunks: List[List[Dict[str, Any]]] = []
    for i in range(0, len(word_timings), max_words_per_line):
        chunks.append(word_timings[i : i + max_words_per_line])
    return chunks


def _build_subtitle_timeline(
    word_timings: List[Dict[str, Any]], max_words_per_line: int, highlight_color: str
) -> List[Dict[str, Any]]:
    """
    Build the full word-by-word, animated-highlight subtitle timeline.

    Args:
        word_timings: Ordered list of word timing dicts.
        max_words_per_line: Maximum words allowed per subtitle line.
        highlight_color: Hex color used to mark the "active" word.

    Returns:
        A list of subtitle line dicts.
    """
    timeline: List[Dict[str, Any]] = []

    for line_id, chunk in enumerate(_chunk_word_timings(word_timings, max_words_per_line), start=1):
        words = [
            {
                "word": w["word"],
                "start": w["start"],
                "end": w["end"],
                "highlight_color": highlight_color,
            }
            for w in chunk
        ]
        timeline.append(
            {
                "line_id": line_id,
                "text": " ".join(w["word"] for w in chunk),
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
                "words": words,
            }
        )

    return timeline


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a word-by-word subtitle timeline from word timings.

    Args:
        input_json: Must contain "run_id", "topic", and a non-empty
            "word_timings" list.

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "word_timings"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        word_timings = input_json["word_timings"]

        if not isinstance(word_timings, list) or not word_timings:
            raise ContractError("word_timings must be a non-empty list")

        subtitle_timeline = _build_subtitle_timeline(
            word_timings,
            pipeline_config.SUBTITLE_MAX_WORDS_PER_LINE,
            pipeline_config.SUBTITLE_HIGHLIGHT_COLOR,
        )

        logger.info(
            "Subtitles generated for run_id=%s topic='%s' -> %d lines from %d words",
            run_id,
            topic,
            len(subtitle_timeline),
            len(word_timings),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "subtitle_timeline": subtitle_timeline,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Subtitle Generator contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Subtitle Generator failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
