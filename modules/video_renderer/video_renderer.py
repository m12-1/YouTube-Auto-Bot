"""
modules/video_renderer/video_renderer.py

Renders the final vertical video (1080x1920, 30 FPS) from a render
plan produced by `video_composer`: composites per-scene media with
Ken Burns/animation effects, transitions, the narration audio track,
and animated word-by-word subtitles. Also produces a thumbnail and
metadata/SEO sidecar files.

Uses MoviePy when available and every scene has a resolvable local
media file; otherwise falls back to writing a structured render
manifest (metadata + SEO + a documented "what would be rendered"
plan) so the pipeline can still be exercised and inspected end-to-end
without a fully populated media cache.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "render_plan": {
            "resolution": {"width": int, "height": int}, "fps": int,
            "audio_track": str, "total_duration_seconds": float,
            "tracks": {"video": [...], "subtitles": [...]},
            "missing_media_scene_ids": [str, ...]
        },
        "seo": {"title": str, "description": str, "tags": [...], ...}
    }

output_json:
    {
        "status": "success" | "error",
        "module": "video_renderer",
        "data": {
            "run_id": str,
            "topic": str,
            "final_video_path": str,
            "thumbnail_path": str,
            "metadata_path": str,
            "seo_path": str,
            "rendered": bool,
            "source": "moviepy" | "manifest_only"
        },
        "error": str | null
    }
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry
from shared.path_utils import safe_path, sanitize_filename

logger = get_logger(__name__)

MODULE_NAME = "video_renderer"

try:
    # Pillow >= 10 removed the long-deprecated Image.ANTIALIAS constant, but
    # moviepy 1.x (the last version that still exposes `moviepy.editor`)
    # still references it internally for resizing. Restore it as an alias
    # for the equivalent Image.LANCZOS filter so moviepy keeps working
    # without pinning Pillow to an old version that lacks Python 3.12 wheels.
    from PIL import Image as _PILImage

    if not hasattr(_PILImage, "ANTIALIAS"):
        _PILImage.ANTIALIAS = _PILImage.LANCZOS
except ImportError:  # pragma: no cover - Pillow always present as a dependency
    pass

try:
    from moviepy.editor import (
        AudioFileClip,
        CompositeVideoClip,
        ImageClip,
        TextClip,
        VideoFileClip,
        concatenate_videoclips,
    )

    _MOVIEPY_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without MoviePy installed
    _MOVIEPY_AVAILABLE = False


class RenderError(RuntimeError):
    """Raised when MoviePy rendering cannot be completed."""


# Fonts to try, in order of preference, for burned-in subtitles. "Arial-Bold"
# was previously hardcoded here, but Arial is a Microsoft font that is not
# installed on the GitHub Actions ubuntu-latest runner (or most Linux boxes)
# -- ImageMagick has no such font registered, so every TextClip call failed
# with "unable to read font `Arial-Bold'" and the renderer silently fell
# back to manifest-only output on every run. DejaVu Sans Bold is the actual
# font this project's CI workflow now installs (see
# .github/workflows/Run pipeline.yml), with a couple of other common Linux
# bold-sans fallbacks in case someone runs this on a different distro/image.
_SUBTITLE_FONT_CANDIDATES = (
    "DejaVu-Sans-Bold",
    "DejaVuSans-Bold",
    "Liberation-Sans-Bold",
    "Nimbus-Sans-Bold",
    "FreeSans-Bold",
)

_resolved_subtitle_font: str | None = None
_font_resolution_attempted = False


def _resolve_subtitle_font() -> str | None:
    """
    Pick a bold sans-serif font that's actually registered with ImageMagick
    on this machine, instead of assuming a hardcoded name exists.

    Returns:
        A font name MoviePy's TextClip can use, or None if no candidate
        (and no font at all) could be found -- in which case the caller
        should let TextClip fail fast rather than silently mis-render.
    """
    global _resolved_subtitle_font, _font_resolution_attempted

    if _font_resolution_attempted:
        return _resolved_subtitle_font
    _font_resolution_attempted = True

    try:
        available = {name.lower(): name for name in TextClip.list("font")}
    except Exception:  # noqa: BLE001 - ImageMagick not queryable; fall through
        available = {}

    for candidate in _SUBTITLE_FONT_CANDIDATES:
        match = available.get(candidate.lower())
        if match:
            _resolved_subtitle_font = match
            return _resolved_subtitle_font

    # None of our preferred fonts are registered. Rather than hand TextClip
    # a name we know is wrong, fall back to whatever bold-ish font is
    # available so rendering can still proceed, and log clearly so this is
    # diagnosable instead of surfacing only as an opaque ImageMagick error.
    bold_fallback = next((name for name in available.values() if "bold" in name.lower()), None)
    if bold_fallback:
        logger.warning(
            "None of the preferred subtitle fonts %s are installed; "
            "falling back to '%s'. Consider installing fonts-dejavu-core.",
            _SUBTITLE_FONT_CANDIDATES,
            bold_fallback,
        )
        _resolved_subtitle_font = bold_fallback
        return _resolved_subtitle_font

    logger.warning(
        "No usable font found via ImageMagick (checked %s); subtitle "
        "rendering will use MoviePy/ImageMagick's default and may fail. "
        "Install fonts-dejavu-core (or another bold sans font) on this machine.",
        _SUBTITLE_FONT_CANDIDATES,
    )
    return None


def _run_output_paths(run_id: str) -> Dict[str, str]:
    """Build stable output paths for all render artifacts for this run."""
    safe_digest = sanitize_filename(run_id)
    base_dir = safe_path(pipeline_config.VIDEO_OUTPUT_DIR, safe_digest)
    return {
        "final_video_path": str(safe_path(base_dir, "final.mp4")),
        "thumbnail_path": str(safe_path(base_dir, "thumbnail.jpg")),
        "metadata_path": str(safe_path(base_dir, "metadata.json")),
        "seo_path": str(safe_path(base_dir, "seo.json")),
    }


def _scene_clip(scene: Dict[str, Any], width: int, height: int):
    """
    Build a single MoviePy clip for one scene, applying a Ken Burns
    zoom/pan effect when enabled and the media is a still image.

    Args:
        scene: A video track entry from the render plan.
        width: Target frame width.
        height: Target frame height.

    Returns:
        A MoviePy clip sized/positioned for the vertical frame.

    Raises:
        RenderError: If the scene has no resolvable media file.
    """
    media_path = scene.get("media_path")
    duration = max(scene["end"] - scene["start"], 0.1)

    if not media_path or not os.path.isfile(media_path):
        raise RenderError(f"Scene {scene['scene_id']} has no local media file to render.")

    is_video = media_path.lower().endswith((".mp4", ".mov", ".webm"))

    if is_video:
        clip = VideoFileClip(media_path).subclip(0, min(duration, VideoFileClip(media_path).duration))
    else:
        clip = ImageClip(media_path).set_duration(duration)
        if scene.get("ken_burns"):
            zoom_start, zoom_end = 1.0, 1.08
            clip = clip.resize(lambda t: zoom_start + (zoom_end - zoom_start) * (t / duration))

    clip = clip.resize(height=height).set_position("center")
    return clip.set_duration(duration)


def _subtitle_clips(subtitle_lines: List[Dict[str, Any]], width: int, height: int) -> List[Any]:
    """
    Build MoviePy TextClips for every subtitle line, positioned near
    the bottom third of the frame.

    Args:
        subtitle_lines: Subtitle timeline entries.
        width: Target frame width.
        height: Target frame height.

    Returns:
        A list of positioned, timed TextClips.
    """
    clips = []
    font = _resolve_subtitle_font()
    text_clip_kwargs: Dict[str, Any] = {"fontsize": 64, "color": "white", "stroke_color": "black", "stroke_width": 2}
    if font:
        text_clip_kwargs["font"] = font

    for line in subtitle_lines:
        text_clip = (
            TextClip(line["text"], **text_clip_kwargs)
            .set_start(line["start"])
            .set_duration(line["end"] - line["start"])
            .set_position(("center", height * 0.75))
        )
        clips.append(text_clip)
    return clips


@retry(max_attempts=1, exceptions=(RenderError,))
def _render_with_moviepy(
    render_plan: Dict[str, Any], output_paths: Dict[str, str]
) -> None:
    """
    Render the final video using MoviePy: scene clips, transitions,
    narration audio, and animated subtitles composited together.

    Args:
        render_plan: The render plan produced by `video_composer`.
        output_paths: Destination paths for the final video/thumbnail.

    Raises:
        RenderError: If any scene is missing media or rendering fails.
    """
    width = render_plan["resolution"]["width"]
    height = render_plan["resolution"]["height"]
    fps = render_plan["fps"]

    os.makedirs(os.path.dirname(output_paths["final_video_path"]), exist_ok=True)

    try:
        scene_clips = [_scene_clip(scene, width, height) for scene in render_plan["tracks"]["video"]]
        video = concatenate_videoclips(scene_clips, method="compose")

        subtitle_clips = _subtitle_clips(render_plan["tracks"]["subtitles"], width, height)
        composite = CompositeVideoClip([video, *subtitle_clips], size=(width, height))

        audio_path = render_plan.get("audio_track")
        if audio_path and os.path.isfile(audio_path):
            composite = composite.set_audio(AudioFileClip(audio_path))

        composite.write_videofile(
            output_paths["final_video_path"], fps=fps, codec="libx264", audio_codec="aac", logger=None
        )
        composite.save_frame(output_paths["thumbnail_path"], t=min(0.5, composite.duration))

    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenderError(str(exc)) from exc


def _write_manifest_only(
    render_plan: Dict[str, Any], seo: Dict[str, Any], output_paths: Dict[str, str]
) -> None:
    """
    Write metadata/SEO sidecar files (and a documented render manifest
    in place of `final.mp4`) when MoviePy rendering can't proceed —
    e.g. missing scene media or MoviePy not installed.

    Args:
        render_plan: The render plan produced by `video_composer`.
        seo: SEO metadata produced by `seo_generator`.
        output_paths: Destination paths for all artifacts.
    """
    os.makedirs(os.path.dirname(output_paths["final_video_path"]), exist_ok=True)

    manifest = {
        "note": (
            "MoviePy rendering was skipped (missing media file(s) and/or "
            "MoviePy not installed in this environment). This manifest "
            "documents exactly what would have been rendered."
        ),
        "render_plan": render_plan,
    }
    with open(output_paths["final_video_path"] + ".manifest.json", "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)


def _write_sidecar_files(
    run_id: str,
    topic: str,
    render_plan: Dict[str, Any],
    seo: Dict[str, Any],
    output_paths: Dict[str, str],
) -> None:
    """
    Write `metadata.json` and `seo.json` sidecar files for this run.

    Args:
        run_id: The pipeline run id.
        topic: The video topic.
        render_plan: The render plan produced by `video_composer`.
        seo: SEO metadata produced by `seo_generator`.
        output_paths: Destination paths for all artifacts.
    """
    metadata = {
        "run_id": run_id,
        "topic": topic,
        "resolution": render_plan["resolution"],
        "fps": render_plan["fps"],
        "duration_seconds": render_plan.get("total_duration_seconds"),
        "scene_count": len(render_plan["tracks"]["video"]),
        "missing_media_scene_ids": render_plan.get("missing_media_scene_ids", []),
    }

    os.makedirs(os.path.dirname(output_paths["metadata_path"]), exist_ok=True)
    with open(output_paths["metadata_path"], "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)

    with open(output_paths["seo_path"], "w", encoding="utf-8") as handle:
        json.dump(seo, handle, ensure_ascii=False, indent=2)


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render the final video (or a documented manifest fallback) plus
    metadata/SEO sidecar files.

    Args:
        input_json: Must contain "run_id", "topic", "render_plan", and "seo".

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "render_plan", "seo"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        render_plan = input_json["render_plan"]
        seo = input_json["seo"]

        if not isinstance(render_plan, dict):
            raise ContractError("render_plan must be a dict")

        output_paths = _run_output_paths(run_id)

        rendered = False
        source = "manifest_only"

        can_attempt_render = _MOVIEPY_AVAILABLE and not render_plan.get("missing_media_scene_ids")

        if can_attempt_render:
            try:
                _render_with_moviepy(render_plan, output_paths)
                rendered = True
                source = "moviepy"
            except RenderError as exc:
                logger.warning(
                    "video_renderer falling back to manifest-only output for run_id=%s: %s",
                    run_id,
                    exc,
                )
                _write_manifest_only(render_plan, seo, output_paths)
        else:
            if not _MOVIEPY_AVAILABLE:
                logger.warning("video_renderer: MoviePy is not installed; writing manifest only.")
            else:
                logger.warning(
                    "video_renderer: run_id=%s has scenes with missing media; writing manifest only.",
                    run_id,
                )
            _write_manifest_only(render_plan, seo, output_paths)

        _write_sidecar_files(run_id, topic, render_plan, seo, output_paths)

        logger.info(
            "Video render step complete for run_id=%s topic='%s' rendered=%s source=%s",
            run_id,
            topic,
            rendered,
            source,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            **output_paths,
            "rendered": rendered,
            "source": source,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Video Renderer contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Video Renderer failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
