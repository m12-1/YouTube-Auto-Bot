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

# Try to import OpenCV for video frame extraction; fall back to MoviePy
# if unavailable, and ultimately to no-analysis if neither is present.
try:
    import cv2 as _cv2  # type: ignore[import-untyped]

    _CV2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CV2_AVAILABLE = False

try:
    from moviepy.editor import VideoFileClip as _VFC  # type: ignore[import-untyped]

    _MOVIEPY_FOR_FRAMES = True
except ImportError:  # pragma: no cover
    _MOVIEPY_FOR_FRAMES = False


def _check_resolution(candidate: Dict[str, Any]) -> List[str]:
    """
    Reject candidates whose EFFECTIVE resolution, once fit into the final
    1080x1920 portrait canvas the way `video_renderer` actually does it
    (scale to canvas height, then center-crop the width), would fall
    below 1080p -- and reject anything that would need to be upscaled so
    far it goes soft/blurry.

    A landscape 1920x1080 stock clip is NOT unusable: scaled up to the
    canvas height it becomes plenty wide enough. Rejecting every
    landscape candidate outright (the old orientation check) was the
    direct cause of scenes coming back with 0 usable candidates even
    though valid 1080p+ candidates were downloaded.
    """
    reasons: List[str] = []
    width = candidate.get("width", 0) or 0
    height = candidate.get("height", 0) or 0

    if not width or not height:
        reasons.append("missing_dimensions")
        return reasons

    canvas_width = pipeline_config.MEDIA_MIN_WIDTH  # 1080
    canvas_height = pipeline_config.MEDIA_MIN_HEIGHT  # 1920

    scale = canvas_height / height
    effective_width = width * scale
    effective_height = height * scale

    if effective_width < canvas_width or effective_height < canvas_height:
        reasons.append(
            f"low_resolution_after_fit ({width}x{height} scaled -> "
            f"{effective_width:.0f}x{effective_height:.0f} < {canvas_width}x{canvas_height})"
        )
        return reasons

    if scale > pipeline_config.MEDIA_MAX_UPSCALE_FACTOR:
        reasons.append(
            f"excessive_upscale ({width}x{height} needs {scale:.2f}x to fill the canvas, "
            f"max allowed is {pipeline_config.MEDIA_MAX_UPSCALE_FACTOR:.2f}x)"
        )
    return reasons


def _check_orientation(candidate: Dict[str, Any]) -> List[str]:
    """
    No longer a hard rejection gate. `video_renderer` fits every clip
    (portrait OR landscape) into the 1080x1920 canvas by scaling to the
    canvas height and center-cropping the width, so orientation alone is
    never a valid reason to throw a candidate away. Real quality/fit
    enforcement now lives in `_check_resolution` above. Kept as a no-op
    function (rather than deleted) so `_evaluate_candidate` and any
    external callers/tests referencing it keep working unchanged.
    """
    return []


def _check_duration(candidate: Dict[str, Any]) -> List[str]:
    """Reject video candidates that are shorter than the configured minimum."""
    reasons = []
    duration = candidate.get("duration_seconds")
    if duration is not None and duration < pipeline_config.MEDIA_MIN_DURATION_SECONDS:
        reasons.append(
            f"too_short ({duration}s < {pipeline_config.MEDIA_MIN_DURATION_SECONDS}s)"
        )
    return reasons


def _extract_video_frames(local_path: str) -> List[Any]:
    """
    Extract 3 representative frames from a video file at 10%, 50%, and
    90% of its duration. Tries OpenCV first for speed, then MoviePy as
    a fallback.

    Args:
        local_path: Absolute path to a video file on disk.

    Returns:
        A list of PIL Image objects (may be fewer than 3 if extraction
        partially fails), or an empty list if extraction is not possible.
    """
    positions = [0.10, 0.50, 0.90]  # proportions of total duration
    frames: List[Any] = []

    if _CV2_AVAILABLE:
        cap = _cv2.VideoCapture(local_path)
        try:
            total_frames = int(cap.get(_cv2.CAP_PROP_FRAME_COUNT))
            if total_frames <= 0:
                return []
            for pct in positions:
                target_frame = int(total_frames * pct)
                cap.set(_cv2.CAP_PROP_POS_FRAMES, target_frame)
                ret, frame = cap.read()
                if ret and frame is not None:
                    # Convert BGR (OpenCV) to RGB (PIL)
                    rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
                    frames.append(Image.fromarray(rgb))
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenCV frame extraction failed for '%s': %s", local_path, exc)
        finally:
            cap.release()

        if frames:
            return frames

    # Fallback: MoviePy
    if _MOVIEPY_FOR_FRAMES:
        try:
            clip = _VFC(local_path)
            try:
                dur = clip.duration or 0
                if dur <= 0:
                    return []
                for pct in positions:
                    t = dur * pct
                    frame_arr = clip.get_frame(min(t, dur - 0.01))
                    frames.append(Image.fromarray(frame_arr))
            finally:
                clip.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MoviePy frame extraction failed for '%s': %s", local_path, exc)

    return frames


def _analyze_single_frame(img: Any) -> Dict[str, float]:
    """
    Compute quality metrics for a single PIL Image (from an image file
    or an extracted video frame).

    Returns:
        A dict with keys: brightness, blur_variance, contrast,
        watermark_ratio, compression_edge_ratio.
    """
    grayscale = img.convert("L")
    stat = ImageStat.Stat(grayscale)

    brightness = stat.mean[0]
    contrast = stat.stddev[0]  # standard deviation of pixel intensities

    # Blur proxy: variance of edge-detected image
    edges = grayscale.filter(ImageFilter.FIND_EDGES)
    blur_variance = ImageStat.Stat(edges).var[0]

    # Watermark heuristic: check the four corners for near-white regions
    w, h = grayscale.size
    corner_size = max(1, int(min(w, h) * 0.08))
    corners = [
        grayscale.crop((0, 0, corner_size, corner_size)),
        grayscale.crop((w - corner_size, 0, w, corner_size)),
        grayscale.crop((0, h - corner_size, corner_size, h)),
        grayscale.crop((w - corner_size, h - corner_size, w, h)),
    ]
    near_white_pixels = 0
    total_corner_pixels = 0
    for corner in corners:
        pixels = list(corner.getdata())
        total_corner_pixels += len(pixels)
        near_white_pixels += sum(1 for p in pixels if p > 240)
    watermark_ratio = near_white_pixels / max(1, total_corner_pixels)

    # Compression artifacts: high-frequency blockiness in 8x8 DCT grids.
    # Proxy: apply an edge filter, then measure variance of the edge
    # image — heavy JPEG/H.264 blocking produces unnaturally uniform
    # 8-pixel-wide edges. We measure the ratio of very-low-variance
    # 8×8 blocks as a proxy for block artifacts.
    edge_data = list(edges.getdata())
    block_count = 0
    low_var_blocks = 0
    block_size = 8
    for row in range(0, h - block_size, block_size):
        for col in range(0, w - block_size, block_size):
            block_pixels = []
            for r in range(block_size):
                idx = (row + r) * w + col
                block_pixels.extend(edge_data[idx : idx + block_size])
            block_count += 1
            if block_pixels:
                mean_b = sum(block_pixels) / len(block_pixels)
                var_b = sum((p - mean_b) ** 2 for p in block_pixels) / len(block_pixels)
                if var_b < pipeline_config.MEDIA_COMPRESSION_ARTIFACT_THRESHOLD:
                    low_var_blocks += 1
    compression_edge_ratio = low_var_blocks / max(1, block_count)

    return {
        "brightness": brightness,
        "blur_variance": blur_variance,
        "contrast": contrast,
        "watermark_ratio": watermark_ratio,
        "compression_edge_ratio": compression_edge_ratio,
    }


def _analyze_image_file(local_path: str) -> Tuple[List[str], bool]:
    """
    Run blur/brightness/contrast/watermark heuristics against an actual
    image file on disk.

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
            metrics = _analyze_single_frame(img)

            reasons = []
            if metrics["brightness"] < pipeline_config.MEDIA_BRIGHTNESS_MIN:
                reasons.append(f"too_dark (brightness={metrics['brightness']:.1f})")
            if metrics["blur_variance"] < pipeline_config.MEDIA_BLUR_THRESHOLD:
                reasons.append(f"blurry (edge_variance={metrics['blur_variance']:.1f})")
            if metrics["contrast"] < pipeline_config.MEDIA_CONTRAST_MIN:
                reasons.append(f"low_contrast (contrast={metrics['contrast']:.1f})")
            if metrics["watermark_ratio"] > pipeline_config.MEDIA_WATERMARK_CORNER_RATIO:
                reasons.append(f"possible_watermark (corner_ratio={metrics['watermark_ratio']:.2f})")

            return reasons, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not analyze image file '%s': %s", local_path, exc)
        return [], False


def _analyze_video_file(local_path: str) -> Tuple[List[str], bool]:
    """
    Run quality analysis on a video file by extracting frames at 10%,
    50%, and 90% of its duration and averaging their quality metrics.

    Checks: blur, brightness, contrast, watermark, compression artifacts.

    Args:
        local_path: Path to the downloaded video file.

    Returns:
        A tuple of (rejection_reasons, analyzed).
    """
    if not _PIL_AVAILABLE:
        return [], False

    if not os.path.isfile(local_path):
        return [], False

    frames = _extract_video_frames(local_path)
    if not frames:
        logger.warning(
            "Could not extract frames from video '%s'; skipping visual analysis.", local_path
        )
        return [], False

    try:
        # Analyze each frame and average the metrics
        all_metrics = [_analyze_single_frame(f) for f in frames]
        n = len(all_metrics)
        avg = {
            key: sum(m[key] for m in all_metrics) / n
            for key in all_metrics[0]
        }

        reasons = []
        if avg["brightness"] < pipeline_config.MEDIA_BRIGHTNESS_MIN:
            reasons.append(f"too_dark (avg_brightness={avg['brightness']:.1f})")
        if avg["blur_variance"] < pipeline_config.MEDIA_BLUR_THRESHOLD:
            reasons.append(f"blurry (avg_edge_variance={avg['blur_variance']:.1f})")
        if avg["contrast"] < pipeline_config.MEDIA_CONTRAST_MIN:
            reasons.append(f"low_contrast (avg_contrast={avg['contrast']:.1f})")
        if avg["watermark_ratio"] > pipeline_config.MEDIA_WATERMARK_CORNER_RATIO:
            reasons.append(f"possible_watermark (avg_corner_ratio={avg['watermark_ratio']:.2f})")

        return reasons, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Video frame analysis failed for '%s': %s", local_path, exc)
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
        visual_reasons, analyzed = _analyze_video_file(local_path)
    else:
        visual_reasons, analyzed = _analyze_image_file(local_path)
    reasons += visual_reasons

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
