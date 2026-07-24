"""
modules/media_planner/media_planner.py

Converts a storyboard into concrete media requirements: for every
scene, decide whether to source a video or an image, its target
duration, priority, primary/alternative search keywords, and a camera
movement suggestion.

Deterministic implementation — driven entirely by storyboard content
and `config.pipeline_config` values, no AI call required.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "storyboard": [
            {
                "scene_id": str, "start_time": float, "end_time": float,
                "narration": str, "visual_description": str,
                "keywords": [str, ...], "animation": str, "transition": str
            },
            ...
        ]
    }

output_json:
    {
        "status": "success" | "error",
        "module": "media_planner",
        "data": {
            "run_id": str,
            "topic": str,
            "media_plan": [
                {
                    "scene_id": str,
                    "media_type": "video" | "image",
                    "duration_seconds": float,
                    "priority": "high" | "medium" | "low",
                    "search_keywords": [str, ...],
                    "alternative_keywords": [str, ...],
                    "camera_movement": str
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

MODULE_NAME = "media_planner"

# Scenes longer than this favor video clips (more visual motion needed);
# shorter scenes work fine with a still image plus a Ken Burns pan/zoom.
_VIDEO_DURATION_THRESHOLD_SECONDS = 4.0


def _decide_media_type(duration_seconds: float) -> str:
    """
    Decide whether a scene should use a video clip or a still image.

    Args:
        duration_seconds: The scene's on-screen duration.

    Returns:
        "video" if the scene is long enough to need motion footage,
        otherwise "image".
    """
    return "video" if duration_seconds >= _VIDEO_DURATION_THRESHOLD_SECONDS else "image"


def _decide_priority(scene_index: int, total_scenes: int) -> str:
    """
    Decide media-sourcing priority for a scene. The hook (first scene)
    and the closer (last scene) get "high" priority since they carry
    the most weight for retention.

    Args:
        scene_index: Zero-based index of this scene in the storyboard.
        total_scenes: Total number of scenes.

    Returns:
        "high", "medium", or "low".
    """
    if scene_index == 0 or scene_index == total_scenes - 1:
        return "high"
    if scene_index < max(2, total_scenes // 3):
        return "medium"
    return "medium"


def _build_alternative_keywords(primary_keywords: List[str], topic: str) -> List[str]:
    """
    Build a broader fallback keyword list in case the primary keywords
    return no usable media from Pexels/Pixabay.

    Args:
        primary_keywords: The scene's primary search keywords.
        topic: The overall video topic, used as a last-resort fallback.

    Returns:
        A list of alternative keyword strings.
    """
    alternatives = list(primary_keywords[1:]) if len(primary_keywords) > 1 else []
    topic_words = [w for w in topic.lower().split() if len(w) > 3]
    for word in topic_words:
        if word not in alternatives:
            alternatives.append(word)
    return alternatives[: pipeline_config.MEDIA_ALTERNATIVE_KEYWORD_COUNT]


def _pick_camera_movement(scene_index: int) -> str:
    """Pick a deterministic camera movement, cycling through configured options."""
    movements = pipeline_config.CAMERA_MOVEMENTS or ["static"]
    return movements[scene_index % len(movements)]


def _build_media_plan(
    storyboard: List[Dict[str, Any]], topic: str
) -> List[Dict[str, Any]]:
    """
    Build a media plan entry for every scene in the storyboard.

    Args:
        storyboard: List of storyboard scene dicts.
        topic: The overall video topic.

    Returns:
        A list of media plan dicts, one per scene.
    """
    plan: List[Dict[str, Any]] = []
    total_scenes = len(storyboard)

    for index, scene in enumerate(storyboard):
        duration = round(scene.get("end_time", 0.0) - scene.get("start_time", 0.0), 2)
        keywords = scene.get("keywords", []) or [topic.lower()]

        plan.append(
            {
                "scene_id": scene["scene_id"],
                "media_type": _decide_media_type(duration),
                "duration_seconds": duration,
                "priority": _decide_priority(index, total_scenes),
                "search_keywords": keywords,
                "alternative_keywords": _build_alternative_keywords(keywords, topic),
                "camera_movement": _pick_camera_movement(index),
            }
        )

    return plan


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a media plan from a storyboard.

    Args:
        input_json: Must contain "run_id", "topic", and a non-empty
            "storyboard" list.

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "storyboard"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        storyboard = input_json["storyboard"]

        if not isinstance(storyboard, list) or not storyboard:
            raise ContractError("storyboard must be a non-empty list")

        media_plan = _build_media_plan(storyboard, topic)

        logger.info(
            "Media plan built for run_id=%s topic='%s' -> %d scenes planned",
            run_id,
            topic,
            len(media_plan),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "media_plan": media_plan,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Media Planner contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Media Planner failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
