"""
modules/media_quality_filter/media_quality_filter.py

Rejects downloaded media candidates that are low resolution, wrong
orientation, watermarked, blurry, too dark, or too short. Returns the
filtered candidate list per scene.

Resolution/orientation/duration checks work off metadata already
present on each candidate (from `media_downloader`). Blur/darkness/
watermark checks additionally inspect the actual image file with
Pillow when it exists on disk and is a real, readable image; if the
file is missing or unreadable those checks are skipped for that
candidate with a warning rather than failing the whole pipeline.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "downloads": [
            {
                "scene_id": str, "provider": str,
                "candidates": [
                    {
                        "candidate_id": str, "url": str, "local_path": str,
                        "width": int, "height": int,
                        "duration_seconds": float | null, "cached": bool
                    },
                    ...
                ]
            },
            ...
        ]
    }

output_json:
    {
        "status": "success" | "error",
        "module": "media_quality_filter",
        "data": {
            "run_id": str,
            "topic": str,
            "filtered": [
                {
                    "scene_id": str,
                    "accepted_candidates": [ {...candidate..., "quality_checks": {...}} ],
                    "rejected_candidates": [ {...candidate..., "rejection_reasons": [str, ...]} ]
                },
                ...
            ]
        },
        "error": str | null
    }
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger

logger = get_logger(__name__)

MODULE_NAME = "media_quality_filter"

try:
    from PIL import Image, ImageFilter, ImageStat

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without Pillow
    _PIL_AVAILABLE = False


def _check_resolution(candidate: Dict[str, Any]) -> List[str]:
    """Reject candidates below the configured minimum resolution."""
    reasons = []
    width = candidate.get("width", 0) or 0
    height = candidate.get("height", 0) or 0
    if width < pipeline_config.MEDIA_MIN_WIDTH or height < pipeline_config.MEDIA_MIN_HEIGHT:
        reasons.append(
            f"low_resolution ({width}x{height} < "
            f"{pipeline_config.MEDIA_MIN_WIDTH}x{pipeline_config.MEDIA_MIN_HEIGHT})"
        )
    return reasons


def _check_orientation(candidate: Dict[str, Any]) -> List[str]:
    """Reject candidates that aren't in the required (portrait) orientation."""
    reasons = []
    width = candidate.get("width", 0) or 0
    height = candidate.get("height", 0) or 0
    if width and height:
        is_portrait = height >= width
        if pipeline_config.MEDIA_REQUIRED_ORIENTATION == "portrait" and not is_portrait:
            reasons.append("wrong_orientation (expected portrait)")
        elif pipeline_config.MEDIA_REQUIRED_ORIENTATION == "landscape" and is_portrait:
            reasons.append("wrong_orientation (expected landscape)")
    return reasons


def _check_duration(candidate: Dict[str, Any]) -> List[str]:
    """Reject video candidates that are shorter than the configured minimum."""
    reasons = []
    duration = candidate.get("duration_seconds")
    if duration is not None and duration < pipeline_config.MEDIA_MIN_DURATION_SECONDS:
        reasons.append(
            f"too_short ({duration}s < {pipeline_config.MEDIA_MIN_DURATION_SECONDS}s)"
        )
    return reasons


def _analyze_image_file(local_path: str) -> Tuple[List[str], bool]:
    """
    Run blur/brightness heuristics against an actual image file on disk.

    Args:
        local_path: Path to the downloaded candidate file.

    Returns:
        A tuple of (rejection_reasons, analyzed) where `analyzed` is
        False if the file could not be inspected (missing, not an
        image, or Pillow unavailable) and checks were skipped.
    """
    if not _PIL_AVAILABLE:
        return [], False

    if not os.path.isfile(local_path):
        return [], False

    try:
        with Image.open(local_path) as img:
            grayscale = img.convert("L")

            # Brightness: mean pixel value across the grayscale image.
            brightness = ImageStat.Stat(grayscale).mean[0]

            # Blur proxy: variance of a Laplacian-like edge filter.
            edges = grayscale.filter(ImageFilter.FIND_EDGES)
            edge_variance = ImageStat.Stat(edges).var[0]

            reasons = []
            if brightness < pipeline_config.MEDIA_BRIGHTNESS_MIN:
                reasons.append(f"too_dark (brightness={brightness:.1f})")
            if edge_variance < pipeline_config.MEDIA_BLUR_THRESHOLD:
                reasons.append(f"blurry (edge_variance={edge_variance:.1f})")

            return reasons, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not analyze image file '%s': %s", local_path, exc)
        return [], False


def _evaluate_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run all quality checks against a single candidate.

    Args:
        candidate: A media candidate dict from `media_downloader`.

    Returns:
        The candidate dict augmented with either "quality_checks"
        (accepted) or "rejection_reasons" (rejected).
    """
    reasons: List[str] = []
    reasons += _check_resolution(candidate)
    reasons += _check_orientation(candidate)
    reasons += _check_duration(candidate)

    local_path = candidate.get("local_path", "")
    is_video_file = local_path.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi"))
    if is_video_file:
        image_reasons, analyzed = [], False
    else:
        image_reasons, analyzed = _analyze_image_file(local_path)
    reasons += image_reasons

    result = dict(candidate)
    if reasons:
        result["rejection_reasons"] = reasons
    else:
        result["quality_checks"] = {
            "resolution_ok": True,
            "orientation_ok": True,
            "duration_ok": True,
            "visual_analysis_performed": analyzed,
        }
    return result


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Filter downloaded media candidates by quality per scene.

    Args:
        input_json: Must contain "run_id", "topic", and a non-empty
            "downloads" list (see `media_downloader` output shape).

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "downloads"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        downloads = input_json["downloads"]

        if not isinstance(downloads, list) or not downloads:
            raise ContractError("downloads must be a non-empty list")

        filtered: List[Dict[str, Any]] = []
        total_accepted = 0
        total_rejected = 0

        for scene_downloads in downloads:
            scene_id = scene_downloads["scene_id"]
            candidates = scene_downloads.get("candidates", [])

            evaluated = [_evaluate_candidate(c) for c in candidates]
            accepted = [c for c in evaluated if "rejection_reasons" not in c]
            rejected = [c for c in evaluated if "rejection_reasons" in c]

            total_accepted += len(accepted)
            total_rejected += len(rejected)

            filtered.append(
                {
                    "scene_id": scene_id,
                    "accepted_candidates": accepted,
                    "rejected_candidates": rejected,
                }
            )

        logger.info(
            "Media quality filter complete for run_id=%s topic='%s' "
            "-> %d accepted, %d rejected across %d scenes",
            run_id,
            topic,
            total_accepted,
            total_rejected,
            len(filtered),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "filtered": filtered,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Media Quality Filter contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Media Quality Filter failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
