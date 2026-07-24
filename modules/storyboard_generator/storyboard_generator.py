"""
modules/storyboard_generator/storyboard_generator.py

Converts a reviewed script's narration and scene breakdown into a
timed storyboard: a list of scenes, each with a start/end time,
narration slice, visual description, keywords, animation, and
transition.

This is a deterministic implementation — no AI call is required, since
timing and scene structure are derived directly from the script's own
scene_breakdown plus configurable defaults.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "script": {
            "narration": str,
            "scene_breakdown": [
                {"scene_number": int, "description": str, "narration_excerpt": str},
                ...
            ]
        }
    }

output_json:
    {
        "status": "success" | "error",
        "module": "storyboard_generator",
        "data": {
            "run_id": str,
            "topic": str,
            "storyboard": [
                {
                    "scene_id": str,
                    "start_time": float,
                    "end_time": float,
                    "narration": str,
                    "visual_description": str,
                    "keywords": [str, ...],
                    "animation": str,
                    "transition": str
                },
                ...
            ],
            "total_duration_seconds": float
        },
        "error": str | null
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger

logger = get_logger(__name__)

MODULE_NAME = "storyboard_generator"

_STOP_WORDS = {"the", "a", "an", "in", "of", "how", "why", "we", "to", "is", "and", "this", "that"}


def _estimate_scene_duration(narration_excerpt: str) -> float:
    """
    Estimate how long a scene should last on screen based on how many
    words its narration excerpt contains (roughly 2.5 words/second of
    spoken narration), with a configurable floor.

    Args:
        narration_excerpt: The narration text spoken during this scene.

    Returns:
        Estimated scene duration, in seconds.
    """
    word_count = len(narration_excerpt.split())
    estimated = word_count / 2.5
    return max(estimated, pipeline_config.STORYBOARD_DEFAULT_SCENE_SECONDS)


def _extract_scene_keywords(text: str, max_keywords: int = 5) -> List[str]:
    """
    Derive simple search keywords from a scene's description/narration.

    Args:
        text: Combined description + narration text for the scene.
        max_keywords: Maximum number of keywords to return.

    Returns:
        A deduplicated list of lowercase keyword strings.
    """
    words = re.findall(r"[A-Za-z']+", text.lower())
    keywords: List[str] = []
    for word in words:
        if word in _STOP_WORDS or len(word) < 3:
            continue
        if word not in keywords:
            keywords.append(word)
        if len(keywords) >= max_keywords:
            break
    return keywords


def _pick_animation(scene_number: int) -> str:
    """Pick a deterministic animation style for a scene, cycling through options."""
    animations = ["ken_burns", "slide_in", "fade_in", "zoom_pulse"]
    return animations[(scene_number - 1) % len(animations)]


def _pick_transition(scene_number: int) -> str:
    """Pick a deterministic transition style for a scene, cycling through config options."""
    transitions = pipeline_config.VIDEO_TRANSITIONS or ["fade"]
    return transitions[(scene_number - 1) % len(transitions)]


def _build_storyboard(scene_breakdown: List[Dict[str, Any]], run_id: str) -> List[Dict[str, Any]]:
    """
    Build a timed storyboard from a script's scene breakdown.

    Args:
        scene_breakdown: List of scene dicts from the Script Reviewer output.
        run_id: The pipeline run id, used to build stable scene ids.

    Returns:
        A list of storyboard scene dicts.
    """
    storyboard: List[Dict[str, Any]] = []
    cursor = 0.0

    for scene in scene_breakdown:
        scene_number = scene.get("scene_number", len(storyboard) + 1)
        narration_excerpt = scene.get("narration_excerpt", "")
        description = scene.get("description", "")

        duration = _estimate_scene_duration(narration_excerpt)
        start_time = round(cursor, 2)
        end_time = round(cursor + duration, 2)
        cursor = end_time

        storyboard.append(
            {
                "scene_id": f"{run_id}-scene-{scene_number}",
                "start_time": start_time,
                "end_time": end_time,
                "narration": narration_excerpt,
                "visual_description": description,
                "keywords": _extract_scene_keywords(f"{description} {narration_excerpt}"),
                "animation": _pick_animation(scene_number),
                "transition": _pick_transition(scene_number),
            }
        )

    return storyboard


def _rescale_to_narration_length(
    storyboard: List[Dict[str, Any]], full_narration: str
) -> List[Dict[str, Any]]:
    """
    Rescale storyboard scene timings so the total duration matches the
    actual narrated text length, not the sum of per-scene excerpt
    estimates.

    AI-generated scripts sometimes give scene_breakdown excerpts that
    don't exactly match the "narration" field that voice_generator
    actually narrates (e.g. a scene's excerpt repeats the hook/question
    while "narration" starts later). Left uncorrected, the video track
    ends up longer than the narrated audio, so subtitles (timed to the
    real narration) stop covering the video well before it ends -- this
    is exactly what the Quality Inspector's "no_missing_subtitles" check
    catches. Scaling every scene's start/end time by the same factor
    preserves each scene's *relative* share of screen time while making
    the total match reality.

    Args:
        storyboard: Storyboard scenes with raw (unscaled) timings.
        full_narration: The script's actual "narration" text.

    Returns:
        The same storyboard list, with timings rescaled in place.
    """
    if not storyboard:
        return storyboard

    raw_total = storyboard[-1]["end_time"]
    word_count = len(full_narration.split())
    target_total = word_count / 2.5 if word_count else raw_total

    if raw_total <= 0 or target_total <= 0:
        return storyboard

    scale = target_total / raw_total
    for scene in storyboard:
        scene["start_time"] = round(scene["start_time"] * scale, 2)
        scene["end_time"] = round(scene["end_time"] * scale, 2)

    return storyboard


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a timed storyboard from a reviewed script.

    Args:
        input_json: Must contain "run_id", "topic", and "script"
            (with a non-empty "scene_breakdown").

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "script"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        script = input_json["script"]

        if not isinstance(script, dict):
            raise ContractError("script must be a dict")

        scene_breakdown = script.get("scene_breakdown")
        if not isinstance(scene_breakdown, list) or not scene_breakdown:
            raise ContractError("script.scene_breakdown must be a non-empty list")

        storyboard = _build_storyboard(scene_breakdown, run_id)
        storyboard = _rescale_to_narration_length(storyboard, script.get("narration", ""))
        total_duration = storyboard[-1]["end_time"] if storyboard else 0.0

        logger.info(
            "Storyboard generated for run_id=%s topic='%s' -> %d scenes, %.2fs total",
            run_id,
            topic,
            len(storyboard),
            total_duration,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "storyboard": storyboard,
            "total_duration_seconds": total_duration,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Storyboard Generator contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Storyboard Generator failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
