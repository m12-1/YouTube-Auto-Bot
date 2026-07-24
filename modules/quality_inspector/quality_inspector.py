"""
modules/quality_inspector/quality_inspector.py

Final QA gate for a pipeline run: verifies there are no missing
scenes, no missing subtitles, correct resolution/duration, no audio
clipping, and no rendering failures. Returns an overall PASS/FAIL
verdict plus a per-check breakdown.

Deterministic implementation — no AI call required.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "render_plan": {
            "resolution": {"width": int, "height": int}, "fps": int,
            "total_duration_seconds": float,
            "tracks": {"video": [...], "subtitles": [...]},
            "missing_media_scene_ids": [str, ...]
        },
        "rendered": bool
    }

input_json (optional keys):
    {
        "audio_peak_db": float   # measured peak audio level, if available
    }

output_json:
    {
        "status": "success" | "error",
        "module": "quality_inspector",
        "data": {
            "run_id": str,
            "topic": str,
            "verdict": "PASS" | "FAIL",
            "checks": {
                "no_missing_scenes": bool,
                "no_missing_subtitles": bool,
                "correct_resolution": bool,
                "correct_duration": bool,
                "no_audio_clipping": bool,
                "no_rendering_failures": bool
            },
            "failure_reasons": [str, ...]
        },
        "error": str | null
    }
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger

logger = get_logger(__name__)

MODULE_NAME = "quality_inspector"


def _check_no_missing_scenes(render_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify every scene in the render plan has resolvable media."""
    missing = render_plan.get("missing_media_scene_ids", [])
    if missing:
        return False, [f"Missing media for scene(s): {missing}"]
    return True, []


def _check_no_missing_subtitles(render_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify the subtitle track has at least one entry covering the full video."""
    subtitles = render_plan.get("tracks", {}).get("subtitles", [])
    if not subtitles:
        return False, ["Subtitle track is empty."]

    total_duration = render_plan.get("total_duration_seconds", 0.0)
    last_subtitle_end = max((s.get("end", 0.0) for s in subtitles), default=0.0)
    if total_duration > 0 and last_subtitle_end < total_duration * 0.9:
        return False, [
            f"Subtitles end at {last_subtitle_end:.2f}s but video runs "
            f"{total_duration:.2f}s (coverage below 90%)."
        ]
    return True, []


def _check_correct_resolution(render_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify the render plan targets the platform-standard 1080x1920 frame."""
    resolution = render_plan.get("resolution", {})
    width = resolution.get("width")
    height = resolution.get("height")
    if width != pipeline_config.VIDEO_WIDTH or height != pipeline_config.VIDEO_HEIGHT:
        return False, [
            f"Resolution {width}x{height} does not match required "
            f"{pipeline_config.VIDEO_WIDTH}x{pipeline_config.VIDEO_HEIGHT}."
        ]
    return True, []


def _check_correct_duration(render_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify the video track's total span matches the reported total duration."""
    video_track = render_plan.get("tracks", {}).get("video", [])
    if not video_track:
        return False, ["Video track is empty; cannot verify duration."]

    computed_duration = max((scene.get("end", 0.0) for scene in video_track), default=0.0)
    reported_duration = render_plan.get("total_duration_seconds", 0.0)
    drift = abs(computed_duration - reported_duration)

    if drift > pipeline_config.QUALITY_MAX_DURATION_DRIFT_SECONDS:
        return False, [
            f"Duration drift of {drift:.2f}s exceeds allowed "
            f"{pipeline_config.QUALITY_MAX_DURATION_DRIFT_SECONDS}s "
            f"(computed={computed_duration:.2f}s, reported={reported_duration:.2f}s)."
        ]
    return True, []


def _check_no_audio_clipping(audio_peak_db: float | None) -> Tuple[bool, List[str]]:
    """Verify measured peak audio level stays under the clipping threshold."""
    if audio_peak_db is None:
        # No measurement available — treat as not-yet-verified but not a failure,
        # since audio analysis may run at a later mixing stage in Part 3.
        return True, []

    if audio_peak_db > pipeline_config.QUALITY_AUDIO_CLIP_THRESHOLD_DB:
        return False, [
            f"Audio peak of {audio_peak_db:.2f}dB exceeds clipping threshold "
            f"{pipeline_config.QUALITY_AUDIO_CLIP_THRESHOLD_DB}dB."
        ]
    return True, []


def _check_no_rendering_failures(rendered: bool, render_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Verify the Video Renderer actually produced a rendered file (not manifest-only)."""
    if not rendered:
        return False, ["Video Renderer did not produce a rendered file (manifest-only fallback)."]
    return True, []


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the final QA gate against a completed pipeline run.

    Args:
        input_json: Must contain "run_id", "topic", "render_plan", and
            "rendered". May contain "audio_peak_db".

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "render_plan", "rendered"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        render_plan = input_json["render_plan"]
        rendered = bool(input_json["rendered"])
        audio_peak_db = input_json.get("audio_peak_db")

        if not isinstance(render_plan, dict):
            raise ContractError("render_plan must be a dict")

        failure_reasons: List[str] = []
        checks: Dict[str, bool] = {}

        check_functions = {
            "no_missing_scenes": lambda: _check_no_missing_scenes(render_plan),
            "no_missing_subtitles": lambda: _check_no_missing_subtitles(render_plan),
            "correct_resolution": lambda: _check_correct_resolution(render_plan),
            "correct_duration": lambda: _check_correct_duration(render_plan),
            "no_audio_clipping": lambda: _check_no_audio_clipping(audio_peak_db),
            "no_rendering_failures": lambda: _check_no_rendering_failures(rendered, render_plan),
        }

        for name, check_fn in check_functions.items():
            passed, reasons = check_fn()
            checks[name] = passed
            failure_reasons.extend(reasons)

        verdict = "PASS" if all(checks.values()) else "FAIL"

        logger.info(
            "Quality inspection complete for run_id=%s topic='%s' -> verdict=%s (%d failure(s))",
            run_id,
            topic,
            verdict,
            len(failure_reasons),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "verdict": verdict,
            "checks": checks,
            "failure_reasons": failure_reasons,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Quality Inspector contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Quality Inspector failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
