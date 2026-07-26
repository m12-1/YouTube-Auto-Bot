"""
modules/video_composer/video_composer.py

Combines the storyboard, verified media, narration audio, subtitle
timeline, and per-scene transitions into a single render plan — a
declarative timeline description that `video_renderer` consumes to
actually produce the final video file.

Deterministic implementation — no AI call required. This module never
touches ffmpeg/moviepy directly; it only produces the JSON render plan
(separation of concerns between "what to render" and "how to render it").

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "storyboard": [ {"scene_id": str, "start_time": float, "end_time": float,
                          "animation": str, "transition": str}, ... ],
        "verifications": [ {"scene_id": str, "best_media": {...} | null}, ... ],
        "audio_path": str,
        "subtitle_timeline": [ {...}, ... ]
    }

output_json:
    {
        "status": "success" | "error",
        "module": "video_composer",
        "data": {
            "run_id": str,
            "topic": str,
            "render_plan": {
                "resolution": {"width": int, "height": int},
                "fps": int,
                "audio_track": str,
                "total_duration_seconds": float,
                "tracks": {
                    "video": [ {"scene_id": str, "start": float, "end": float,
                                "media_path": str | null, "animation": str,
                                "transition": str, "ken_burns": bool}, ... ],
                    "subtitles": [ {...subtitle line...}, ... ]
                },
                "missing_media_scene_ids": [str, ...]
            }
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

MODULE_NAME = "video_composer"


def _build_video_track(
    storyboard: List[Dict[str, Any]], verifications_by_scene: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Build the per-scene video track entries, pairing storyboard timing
    with the AI-verified best media for that scene.

    Also collects ``fallback_media_paths`` from each scene's rejected
    candidates (those with a valid ``local_path``), sorted by score
    descending, so ``video_renderer`` can try these before borrowing
    media from a neighboring scene.

    Args:
        storyboard: List of storyboard scene dicts.
        verifications_by_scene: Mapping of scene_id -> verification result.

    Returns:
        A list of video track entries, one per scene.
    """
    track: List[Dict[str, Any]] = []

    for scene in storyboard:
        scene_id = scene["scene_id"]
        verification = verifications_by_scene.get(scene_id, {})
        best_media = verification.get("best_media")

        # A scene whose media came from media_downloader's topic-level
        # fallback (see MEDIA_TOPIC_FALLBACK_ENABLED) is always a still
        # image chosen specifically to carry a slow Ken Burns zoom instead
        # of sitting static or the scene going empty -- so it gets the
        # zoom regardless of the global VIDEO_KEN_BURNS_ENABLED toggle.
        is_topic_fallback = bool((best_media or {}).get("is_topic_fallback"))

        # Collect fallback paths from rejected candidates that still have
        # a downloaded file on disk — these are "not the best" but better
        # than a black placeholder or a borrowed clip from another scene.
        fallback_paths: List[str] = []
        for rej in verification.get("rejected_candidates", []):
            # rejected_candidates in ai_media_verification may be
            # summary dicts (just candidate_id/score/reason) without
            # local_path. Only full candidate dicts carry local_path.
            path = rej.get("local_path")
            if path and isinstance(path, str):
                fallback_paths.append(path)

        track.append(
            {
                "scene_id": scene_id,
                "start": scene.get("start_time", 0.0),
                "end": scene.get("end_time", 0.0),
                "media_path": (best_media or {}).get("local_path"),
                "animation": scene.get("animation", "fade_in"),
                "transition": scene.get("transition", "fade"),
                "ken_burns": True if is_topic_fallback else pipeline_config.VIDEO_KEN_BURNS_ENABLED,
                "fallback_media_paths": fallback_paths,
            }
        )

    return track


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a declarative render plan from all upstream pipeline outputs.

    Args:
        input_json: Must contain "run_id", "topic", "storyboard",
            "verifications", "audio_path", and "subtitle_timeline".

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(
            input_json,
            ["run_id", "topic", "storyboard", "verifications", "audio_path", "subtitle_timeline"],
        )

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        storyboard = input_json["storyboard"]
        verifications = input_json["verifications"]
        audio_path = input_json["audio_path"]
        subtitle_timeline = input_json["subtitle_timeline"]

        if not isinstance(storyboard, list) or not storyboard:
            raise ContractError("storyboard must be a non-empty list")
        if not isinstance(verifications, list):
            raise ContractError("verifications must be a list")
        if not isinstance(subtitle_timeline, list):
            raise ContractError("subtitle_timeline must be a list")

        verifications_by_scene = {v["scene_id"]: v for v in verifications}
        video_track = _build_video_track(storyboard, verifications_by_scene)

        missing_media_scene_ids = [
            entry["scene_id"] for entry in video_track if not entry["media_path"]
        ]
        total_duration = max((s.get("end_time", 0.0) for s in storyboard), default=0.0)

        render_plan = {
            "resolution": {"width": pipeline_config.VIDEO_WIDTH, "height": pipeline_config.VIDEO_HEIGHT},
            "fps": pipeline_config.VIDEO_FPS,
            "audio_track": audio_path,
            "total_duration_seconds": total_duration,
            "tracks": {
                "video": video_track,
                "subtitles": subtitle_timeline,
            },
            "missing_media_scene_ids": missing_media_scene_ids,
        }

        if missing_media_scene_ids:
            logger.warning(
                "video_composer: run_id=%s has %d scene(s) with no verified media: %s",
                run_id,
                len(missing_media_scene_ids),
                missing_media_scene_ids,
            )

        logger.info(
            "Render plan composed for run_id=%s topic='%s' -> %d video scenes, %.2fs total",
            run_id,
            topic,
            len(video_track),
            total_duration,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "render_plan": render_plan,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Video Composer contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Video Composer failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
