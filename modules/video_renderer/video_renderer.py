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
from typing import Any, Dict, List, Optional, Set

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
    from PIL import ImageFilter as _PILImageFilter
    from PIL import ImageStat as _PILImageStat

    if not hasattr(_PILImage, "ANTIALIAS"):
        _PILImage.ANTIALIAS = _PILImage.LANCZOS
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - Pillow always present as a dependency
    _PIL_AVAILABLE = False

try:
    from moviepy.editor import (
        AudioFileClip,
        ColorClip,
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


# ---------------------------------------------------------------------------
# Visual Memory — prevents duplicate clips and enforces style consistency
# ---------------------------------------------------------------------------

class VisualMemory:
    """
    Tracks media already used across the entire video to prevent
    duplicate clips, enforce visual style consistency, and avoid
    repeating the same motion type consecutively.
    """

    def __init__(self) -> None:
        self.used_media_paths: Set[str] = set()
        self.locked_style: Optional[str] = None
        self.last_motion_type: Optional[str] = None

    def is_duplicate(self, media_path: str) -> bool:
        """Return True if this exact media file was already used."""
        return media_path in self.used_media_paths

    def register(self, media_path: str, style: Optional[str] = None, motion_type: Optional[str] = None) -> None:
        """Record a media file as used and optionally lock the visual style."""
        self.used_media_paths.add(media_path)
        if style and self.locked_style is None:
            self.locked_style = style
            logger.info("VisualMemory: locked visual style to '%s' (first scene).", style)
        if motion_type:
            self.last_motion_type = motion_type

    def check_style_consistency(self, candidate_style: Optional[str]) -> bool:
        """Return True if the candidate's style is compatible with the locked style."""
        if not self.locked_style or not candidate_style:
            return True
        return candidate_style.lower() == self.locked_style.lower()


# ---------------------------------------------------------------------------
# Subtitle font resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Smart video segment selection
# ---------------------------------------------------------------------------

def _best_video_start(video_path: str, needed_duration: float) -> float:
    """
    Choose the best starting point inside a video clip instead of always
    starting at t=0 (which often shows fade-ins, camera preparation, or
    empty establishing shots — the worst part of the clip).

    Strategy:
        1. If PIL is available, sample 5 evenly-spaced windows and pick
           the one with the highest combined brightness + edge-sharpness.
        2. If analysis fails, fall back to starting at roughly the middle
           of the available surplus duration.
        3. If the video is shorter than needed, return 0 (use everything).

    Args:
        video_path: Path to the source video file.
        needed_duration: How many seconds the scene requires.

    Returns:
        The best start time in seconds.
    """
    try:
        probe_clip = VideoFileClip(video_path)
        clip_duration = probe_clip.duration or 0
        probe_clip.close()
    except Exception:  # noqa: BLE001
        return 0.0

    if clip_duration <= 0 or clip_duration <= needed_duration:
        return 0.0

    surplus = clip_duration - needed_duration
    # Default: start from the middle of the surplus (avoids first/last seconds)
    fallback_start = surplus * 0.5

    if not _PIL_AVAILABLE:
        return round(fallback_start, 2)

    # Sample 5 candidate windows
    num_samples = 5
    best_score = -1.0
    best_start = fallback_start

    try:
        clip = VideoFileClip(video_path)
        try:
            for i in range(num_samples):
                t_start = (surplus / max(num_samples - 1, 1)) * i
                t_sample = t_start + needed_duration * 0.5  # sample from middle of window
                t_sample = min(t_sample, clip_duration - 0.01)

                frame_arr = clip.get_frame(t_sample)
                img = _PILImage.fromarray(frame_arr)
                gray = img.convert("L")

                brightness = _PILImageStat.Stat(gray).mean[0]
                edges = gray.filter(_PILImageFilter.FIND_EDGES)
                sharpness = _PILImageStat.Stat(edges).var[0]

                # Combined score: we want bright + sharp frames
                score = brightness * 0.3 + sharpness * 0.7
                if score > best_score:
                    best_score = score
                    best_start = t_start
        finally:
            clip.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Smart segment selection failed for '%s': %s; using midpoint fallback.",
            video_path, exc,
        )
        return round(fallback_start, 2)

    return round(best_start, 2)


# ---------------------------------------------------------------------------
# Neighbor media borrowing (with fallback-first logic)
# ---------------------------------------------------------------------------

def _fill_missing_media_from_neighbors(
    video_track: List[Dict[str, Any]],
    visual_memory: Optional[VisualMemory] = None,
) -> List[Dict[str, Any]]:
    """
    For any scene with no resolvable media file, try the scene's own
    fallback_media_paths first (rejected-but-downloaded candidates from
    ai_media_verification).  Only after ALL scene-specific fallbacks are
    exhausted does the function borrow from the nearest neighbor.

    Fallback sequence per missing scene:
        1. Scene's own fallback_media_paths (best → worst by rank score)
        2. Only after exhausting those → nearest neighbor's media

    A VisualMemory instance is consulted to avoid re-using a media path
    that was already assigned to a different scene in this same render.

    Args:
        video_track: The render plan's list of video track entries.
        visual_memory: Optional VisualMemory to prevent duplicates.

    Returns:
        A new list of video track entries with "media_path" backfilled
        where possible. Only entries that changed get a copy; everything
        else is passed through unchanged.
    """

    def _has_media(entry: Dict[str, Any]) -> bool:
        path = entry.get("media_path")
        return bool(path) and os.path.isfile(path)

    filled = [dict(entry) for entry in video_track]
    n = len(filled)

    for i, entry in enumerate(filled):
        if _has_media(entry):
            # Register in visual memory
            if visual_memory and entry.get("media_path"):
                visual_memory.register(entry["media_path"])
            continue

        resolved_path = None
        resolved_source = None

        # Step 1: Try this scene's own fallback_media_paths
        for fallback_path in entry.get("fallback_media_paths", []):
            if not os.path.isfile(fallback_path):
                continue
            if visual_memory and visual_memory.is_duplicate(fallback_path):
                continue
            resolved_path = fallback_path
            resolved_source = "own_fallback"
            break

        # Step 2: Only if no scene-specific fallback worked → borrow from neighbor
        if resolved_path is None:
            # Previous scenes first, then next scenes
            for j in range(i - 1, -1, -1):
                if _has_media(filled[j]):
                    candidate_path = filled[j]["media_path"]
                    if visual_memory and visual_memory.is_duplicate(candidate_path):
                        continue
                    resolved_path = candidate_path
                    resolved_source = filled[j]["scene_id"]
                    break

        if resolved_path is None:
            for j in range(i + 1, n):
                if _has_media(filled[j]):
                    candidate_path = filled[j]["media_path"]
                    if visual_memory and visual_memory.is_duplicate(candidate_path):
                        continue
                    resolved_path = candidate_path
                    resolved_source = filled[j]["scene_id"]
                    break

        if resolved_path is not None:
            if resolved_source == "own_fallback":
                logger.info(
                    "Scene %s has no primary media; using own fallback candidate.",
                    entry.get("scene_id"),
                )
            else:
                logger.warning(
                    "Scene %s has no local media or fallbacks; reusing media from "
                    "scene %s as last resort.",
                    entry.get("scene_id"),
                    resolved_source,
                )
                entry["_media_borrowed"] = True
            entry["media_path"] = resolved_path
            if visual_memory:
                visual_memory.register(resolved_path)

    return filled


# ---------------------------------------------------------------------------
# Scene clip builder (with smart segment selection)
# ---------------------------------------------------------------------------

def _scene_clip(scene: Dict[str, Any], width: int, height: int):
    """
    Build a single MoviePy clip for one scene, applying a Ken Burns
    zoom/pan effect when enabled and the media is a still image.

    Uses smart segment selection for video clips: instead of always
    starting at t=0, picks the best-quality window within the source
    video.

    Args:
        scene: A video track entry from the render plan.
        width: Target frame width.
        height: Target frame height.

    Returns:
        A MoviePy clip sized/positioned for the vertical frame. If the
        track-level fill (see `_fill_missing_media_from_neighbors`)
        couldn't find *any* usable media anywhere in the whole video
        (extremely rare -- every single scene lacking media), this
        falls back to a plain black clip as an absolute last resort so
        rendering can still complete.
    """
    media_path = scene.get("media_path")
    duration = max(scene["end"] - scene["start"], 0.1)

    if not media_path or not os.path.isfile(media_path):
        logger.warning(
            "Scene %s has no local media file anywhere to borrow from; using a "
            "black placeholder clip as a last resort.",
            scene.get("scene_id"),
        )
        return ColorClip(size=(width, height), color=(0, 0, 0)).set_duration(duration)

    is_video = media_path.lower().endswith((".mp4", ".mov", ".webm"))

    if is_video:
        # Smart segment selection: pick the best start point
        best_start = _best_video_start(media_path, duration)
        source_clip = VideoFileClip(media_path)
        source_duration = source_clip.duration or 0
        end_time = min(best_start + duration, source_duration)
        clip = source_clip.subclip(best_start, end_time)
    else:
        clip = ImageClip(media_path).set_duration(duration)
        # A borrowed clip (reused from a neighboring scene) gets its own
        # zoom-out motion, distinct from a fresh scene's zoom-in, so a
        # repeated shot reads as an intentional cutaway rather than a
        # frozen duplicate of the previous frame.
        if scene.get("_media_borrowed"):
            zoom_start, zoom_end = 1.08, 1.0
            clip = clip.resize(lambda t: zoom_start + (zoom_end - zoom_start) * (t / duration))
        elif scene.get("ken_burns"):
            zoom_start, zoom_end = 1.0, 1.08
            clip = clip.resize(lambda t: zoom_start + (zoom_end - zoom_start) * (t / duration))

    clip = clip.resize(height=height).set_position("center")
    return clip.set_duration(duration)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

_SUPPORTED_TRANSITIONS = frozenset({"cut", "fade", "crossfade", "dissolve"})


def _build_video_with_transitions(
    scene_clips: List[Any],
    video_track: List[Dict[str, Any]],
    width: int,
    height: int,
) -> Any:
    """
    Assemble scene clips with real transitions instead of hard-cutting.

    Supported transition types (read from each scene's ``transition`` key):
        - ``cut``       — plain concatenation, no overlap
        - ``fade``      — fadeout on clip A, fadein on clip B
        - ``crossfade`` — A and B overlap with opposing opacity ramps
        - ``dissolve``  — alias for crossfade

    If a scene's ``transition`` key is missing or unrecognized, defaults
    to ``cut``.

    Args:
        scene_clips: Ordered list of MoviePy clips, one per scene.
        video_track: The render plan's video track entries (same order).
        width:  Target frame width.
        height: Target frame height.

    Returns:
        A single MoviePy clip representing all scenes with transitions.
    """
    if not scene_clips:
        return ColorClip(size=(width, height), color=(0, 0, 0)).set_duration(0.1)

    if len(scene_clips) == 1:
        return scene_clips[0]

    trans_duration = max(0.1, pipeline_config.VIDEO_TRANSITION_DURATION_SECONDS)

    # Build the timeline manually: each clip gets a start_offset
    # that accounts for transition overlaps.
    result_clips: List[Any] = []
    current_offset = 0.0

    for idx, clip in enumerate(scene_clips):
        # Determine the transition INTO this scene (from the previous one).
        # The first scene has no incoming transition.
        if idx == 0:
            result_clips.append(clip.set_start(0))
            current_offset = clip.duration
            continue

        # Read transition type from the current scene's track entry
        trans_type = "cut"
        if idx < len(video_track):
            raw = video_track[idx].get("transition", "cut") or "cut"
            trans_type = raw.lower().strip()
            if trans_type not in _SUPPORTED_TRANSITIONS:
                trans_type = "cut"

        prev_clip = result_clips[-1]

        if trans_type == "cut":
            result_clips.append(clip.set_start(current_offset))
            current_offset += clip.duration
        elif trans_type == "fade":
            # Fade: previous clip fades out, current clip fades in
            overlap = min(trans_duration, prev_clip.duration * 0.4, clip.duration * 0.4)
            result_clips[-1] = prev_clip.crossfadeout(overlap)
            new_clip = clip.crossfadein(overlap).set_start(current_offset - overlap)
            result_clips.append(new_clip)
            current_offset += clip.duration - overlap
        elif trans_type in ("crossfade", "dissolve"):
            # Crossfade: both clips overlap with opposing opacity
            overlap = min(trans_duration, prev_clip.duration * 0.4, clip.duration * 0.4)
            result_clips[-1] = prev_clip.crossfadeout(overlap)
            new_clip = clip.crossfadein(overlap).set_start(current_offset - overlap)
            result_clips.append(new_clip)
            current_offset += clip.duration - overlap
        else:
            # Fallback to cut
            result_clips.append(clip.set_start(current_offset))
            current_offset += clip.duration

    return CompositeVideoClip(result_clips, size=(width, height))


# ---------------------------------------------------------------------------
# Subtitle rendering (with word-level highlighting)
# ---------------------------------------------------------------------------

def _word_highlight_clips(
    line: Dict[str, Any],
    width: int,
    height: int,
    font: Optional[str],
    base_kwargs: Dict[str, Any],
) -> List[Any]:
    """
    Build word-level highlight subtitle clips for a single subtitle line.

    For each word in the line, creates:
    - A base TextClip showing the full line text in white (visible for
      the entire line duration)
    - A highlight TextClip showing ONLY the current word in the
      highlight color, positioned on top (visible only during that
      word's time window)

    This creates the "karaoke" effect where the current word lights up
    during narration.

    Args:
        line:        A subtitle timeline entry with "words" list.
        width:       Target frame width.
        height:      Target frame height.
        font:        Resolved font name or None.
        base_kwargs: Common TextClip kwargs (fontsize, colors, etc).

    Returns:
        A list of positioned, timed TextClips.
    """
    clips: List[Any] = []
    full_text = line.get("text", "")
    line_start = line["start"]
    line_end = line["end"]
    line_duration = line_end - line_start
    caption_width = int(width * 0.85)

    if line_duration <= 0 or not full_text:
        return clips

    # Base clip: full text in white for the entire line duration
    base_kw = dict(base_kwargs)
    base_kw["method"] = "caption"
    base_kw["size"] = (caption_width, None)
    if font:
        base_kw["font"] = font

    base_clip = (
        TextClip(full_text, **base_kw)
        .set_start(line_start)
        .set_duration(line_duration)
        .set_position(("center", height * 0.75))
    )
    clips.append(base_clip)

    # Highlight clips: one per word, showing just that word in highlight color
    for word_info in line.get("words", []):
        word_text = word_info.get("word", "")
        w_start = word_info.get("start", line_start)
        w_end = word_info.get("end", line_end)
        highlight_color = word_info.get("highlight_color", "#FFD700")
        w_duration = w_end - w_start

        if w_duration <= 0 or not word_text:
            continue

        highlight_kw = dict(base_kwargs)
        highlight_kw["color"] = highlight_color
        highlight_kw["method"] = "caption"
        highlight_kw["size"] = (caption_width, None)
        if font:
            highlight_kw["font"] = font

        # Build the full-line text but with only the target word visible.
        # Replace non-target words with spaces of matching length to keep
        # horizontal alignment — MoviePy's TextClip uses ImageMagick for
        # layout, so spacing is approximate but sufficient for a karaoke
        # overlay where the base white text is always visible underneath.
        words_in_line = full_text.split()
        masked_parts = []
        for w in words_in_line:
            if w == word_text and word_text not in masked_parts:
                masked_parts.append(w)
            else:
                masked_parts.append(" " * len(w))

        masked_text = " ".join(masked_parts)

        highlight_clip = (
            TextClip(masked_text, **highlight_kw)
            .set_start(w_start)
            .set_duration(w_duration)
            .set_position(("center", height * 0.75))
        )
        clips.append(highlight_clip)

    return clips


def _subtitle_clips(subtitle_lines: List[Dict[str, Any]], width: int, height: int) -> List[Any]:
    """
    Build MoviePy TextClips for every subtitle line, positioned near
    the bottom third of the frame.

    If a line has word-level timing data (``words`` list), creates
    animated word-by-word highlights. Otherwise falls back to a single
    TextClip per line.

    Uses ``method="caption"`` with ``size=(width*0.85, None)`` to
    ensure text wraps within the frame on vertical (Shorts) video.

    Args:
        subtitle_lines: Subtitle timeline entries.
        width: Target frame width.
        height: Target frame height.

    Returns:
        A list of positioned, timed TextClips.
    """
    clips = []
    font = _resolve_subtitle_font()
    caption_width = int(width * 0.85)
    text_clip_kwargs: Dict[str, Any] = {
        "fontsize": 64,
        "color": "white",
        "stroke_color": "black",
        "stroke_width": 2,
        "method": "caption",
        "size": (caption_width, None),
    }
    if font:
        text_clip_kwargs["font"] = font

    for line in subtitle_lines:
        words = line.get("words", [])
        if words:
            # Word-level highlight mode
            clips.extend(
                _word_highlight_clips(line, width, height, font, {
                    "fontsize": 64,
                    "color": "white",
                    "stroke_color": "black",
                    "stroke_width": 2,
                })
            )
        else:
            # Fallback: single TextClip per line
            text_clip = (
                TextClip(line["text"], **text_clip_kwargs)
                .set_start(line["start"])
                .set_duration(line["end"] - line["start"])
                .set_position(("center", height * 0.75))
            )
            clips.append(text_clip)
    return clips


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

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
        visual_memory = VisualMemory()
        video_track = _fill_missing_media_from_neighbors(
            render_plan["tracks"]["video"], visual_memory
        )
        scene_clips = [_scene_clip(scene, width, height) for scene in video_track]

        # Apply real transitions instead of hard cuts
        video = _build_video_with_transitions(scene_clips, video_track, width, height)

        subtitle_clips = _subtitle_clips(render_plan["tracks"]["subtitles"], width, height)
        composite = CompositeVideoClip([video, *subtitle_clips], size=(width, height))

        audio_path = render_plan.get("audio_track")
        if audio_path and os.path.isfile(audio_path):
            composite = composite.set_audio(AudioFileClip(audio_path))

        composite.write_videofile(
            output_paths["final_video_path"], fps=fps, codec="libx264", audio_codec="aac", logger=None
        )
        # withmask=False: the composite includes TextClip subtitle overlays,
        # which carry an alpha mask. save_frame's default (withmask=True)
        # would produce an RGBA frame, and JPEG has no alpha channel, so
        # saving thumbnail.jpg always failed with "cannot write mode RGBA
        # as JPEG". The mask is only needed for video compositing, not for
        # a flattened thumbnail still image.
        composite.save_frame(
            output_paths["thumbnail_path"], t=min(0.5, composite.duration), withmask=False
        )

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

        can_attempt_render = _MOVIEPY_AVAILABLE

        if render_plan.get("missing_media_scene_ids"):
            logger.warning(
                "video_renderer: run_id=%s has scenes with missing media (%s); "
                "rendering with placeholder clip(s) for those scenes instead of "
                "aborting the whole render.",
                run_id,
                render_plan.get("missing_media_scene_ids"),
            )

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
            logger.warning("video_renderer: MoviePy is not installed; writing manifest only.")
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
