"""
config/pipeline_config.py

Non-secret, module-agnostic configuration for the content generation
engine introduced in Part 2. Secrets stay in `config/settings.py` —
this file only holds structural/tunable values (durations, resolution,
thresholds, cache locations) so nothing is hardcoded inside modules.

Every value can be overridden via an environment variable, following
the same pattern as `config/settings.py`.
"""

from __future__ import annotations

import os
from typing import List, Optional


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read an environment variable.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is not set.

    Returns:
        The environment variable's value, or `default` if unset.
    """
    return os.environ.get(name, default)


def _get_env_int(name: str, default: int) -> int:
    """Read an environment variable as an int, falling back to `default`."""
    raw = _get_env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _get_env_float(name: str, default: float) -> float:
    """Read an environment variable as a float, falling back to `default`."""
    raw = _get_env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _get_env_list(name: str, default: List[str]) -> List[str]:
    """Read a comma-separated environment variable as a list of strings."""
    raw = _get_env(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Script Generator / Script Reviewer
# ---------------------------------------------------------------------------
SCRIPT_MAX_SCENES: int = _get_env_int("SCRIPT_MAX_SCENES", 8)
SCRIPT_TARGET_WORD_COUNT: int = _get_env_int("SCRIPT_TARGET_WORD_COUNT", 150)
SCRIPT_MIN_QUALITY_SCORE: float = _get_env_float("SCRIPT_MIN_QUALITY_SCORE", 0.7)

# ---------------------------------------------------------------------------
# SEO Generator
# ---------------------------------------------------------------------------
SEO_MAX_TAGS: int = _get_env_int("SEO_MAX_TAGS", 15)
SEO_MAX_HASHTAGS: int = _get_env_int("SEO_MAX_HASHTAGS", 5)
SEO_TITLE_MAX_LENGTH: int = _get_env_int("SEO_TITLE_MAX_LENGTH", 100)

# ---------------------------------------------------------------------------
# Storyboard / Media Planner
# ---------------------------------------------------------------------------
STORYBOARD_DEFAULT_SCENE_SECONDS: float = _get_env_float(
    "STORYBOARD_DEFAULT_SCENE_SECONDS", 3.0
)
MEDIA_ALTERNATIVE_KEYWORD_COUNT: int = _get_env_int(
    "MEDIA_ALTERNATIVE_KEYWORD_COUNT", 3
)
CAMERA_MOVEMENTS: List[str] = _get_env_list(
    "CAMERA_MOVEMENTS", ["zoom_in", "zoom_out", "pan_left", "pan_right", "static"]
)

# ---------------------------------------------------------------------------
# Media Downloader
# ---------------------------------------------------------------------------
MEDIA_CACHE_DIR: str = _get_env("MEDIA_CACHE_DIR", "./cache/media") or "./cache/media"
MEDIA_DOWNLOAD_MAX_ATTEMPTS: int = _get_env_int("MEDIA_DOWNLOAD_MAX_ATTEMPTS", 3)
MEDIA_DOWNLOAD_TIMEOUT_SECONDS: float = _get_env_float(
    "MEDIA_DOWNLOAD_TIMEOUT_SECONDS", 15.0
)
MEDIA_PROVIDER_PRIORITY: List[str] = _get_env_list(
    "MEDIA_PROVIDER_PRIORITY", ["pexels", "pixabay"]
)

# ---------------------------------------------------------------------------
# Media Quality Filter
# ---------------------------------------------------------------------------
MEDIA_MIN_WIDTH: int = _get_env_int("MEDIA_MIN_WIDTH", 1080)
MEDIA_MIN_HEIGHT: int = _get_env_int("MEDIA_MIN_HEIGHT", 1920)
MEDIA_REQUIRED_ORIENTATION: str = _get_env("MEDIA_REQUIRED_ORIENTATION", "portrait") or "portrait"
MEDIA_MIN_DURATION_SECONDS: float = _get_env_float("MEDIA_MIN_DURATION_SECONDS", 2.0)
MEDIA_BLUR_THRESHOLD: float = _get_env_float("MEDIA_BLUR_THRESHOLD", 100.0)
MEDIA_BRIGHTNESS_MIN: float = _get_env_float("MEDIA_BRIGHTNESS_MIN", 25.0)

# ---------------------------------------------------------------------------
# Media Ranker (Semantic Ranking -- Stage 5)
# ---------------------------------------------------------------------------
# How many raw candidates media_downloader should fetch per scene per
# search attempt before quality filtering / ranking narrows them down.
MEDIA_MAX_CANDIDATES: int = _get_env_int("MEDIA_MAX_CANDIDATES", 15)

# How many of the top-ranked candidates get forwarded to Gemini Vision.
# Gemini must never see the full candidate pool -- only this slice.
MEDIA_MAX_VERIFIED_CANDIDATES: int = _get_env_int("MEDIA_MAX_VERIFIED_CANDIDATES", 5)

# Minimum final_rank_score a candidate needs to even be considered for
# AI verification. Candidates below this are set aside as low-rank.
#
# Deliberately low (0.20). Ranking's job is coarse pre-filtering --
# picking the best MEDIA_MAX_VERIFIED_CANDIDATES to hand to Gemini,
# not making the final semantic judgment call. Its sub-scores are
# blind word-overlap against whatever text a stock provider happened
# to tag a clip with; for abstract or conceptual scenes (quantum
# physics, historical concepts, psychology) that text frequently
# shares almost no literal vocabulary with Scene Analyzer's required
# objects/domain/camera terms even when the clip is visually a great
# match -- Gemini Vision actually looks at the media and is the real
# semantic check, gated separately by MEDIA_MIN_VERIFICATION_SCORE.
# Setting this threshold high starves Gemini of candidates it might
# have approved; keep it just high enough to drop clear zero-signal
# noise, and let MEDIA_MAX_VERIFIED_CANDIDATES cap the field size.
MEDIA_MIN_RANK_SCORE: float = _get_env_float("MEDIA_MIN_RANK_SCORE", 0.20)

# Fallback value used for a ranking sub-score when the signal it would
# compare against is missing/empty (e.g. no camera_movement configured),
# so an absent signal never silently drags a candidate's score to zero.
MEDIA_RANK_NEUTRAL_BASELINE: float = _get_env_float("MEDIA_RANK_NEUTRAL_BASELINE", 0.5)

# Relative weights combined into final_rank_score. They do not need to
# sum to 1.0 -- media_ranker normalizes by their total automatically,
# so any weight can be tuned independently.
MEDIA_RANK_WEIGHT_SEMANTIC_SIMILARITY: float = _get_env_float(
    "MEDIA_RANK_WEIGHT_SEMANTIC_SIMILARITY", 0.20
)
MEDIA_RANK_WEIGHT_SCIENTIFIC_DOMAIN: float = _get_env_float(
    "MEDIA_RANK_WEIGHT_SCIENTIFIC_DOMAIN", 0.10
)
MEDIA_RANK_WEIGHT_REQUIRED_OBJECTS: float = _get_env_float(
    "MEDIA_RANK_WEIGHT_REQUIRED_OBJECTS", 0.15
)
MEDIA_RANK_WEIGHT_REQUIRED_ACTIONS: float = _get_env_float(
    "MEDIA_RANK_WEIGHT_REQUIRED_ACTIONS", 0.10
)
MEDIA_RANK_WEIGHT_ENVIRONMENT: float = _get_env_float("MEDIA_RANK_WEIGHT_ENVIRONMENT", 0.10)
MEDIA_RANK_WEIGHT_CAMERA_STYLE: float = _get_env_float("MEDIA_RANK_WEIGHT_CAMERA_STYLE", 0.05)
MEDIA_RANK_WEIGHT_VISUAL_THEME: float = _get_env_float("MEDIA_RANK_WEIGHT_VISUAL_THEME", 0.08)
MEDIA_RANK_WEIGHT_RESOLUTION: float = _get_env_float("MEDIA_RANK_WEIGHT_RESOLUTION", 0.08)
MEDIA_RANK_WEIGHT_ORIENTATION: float = _get_env_float("MEDIA_RANK_WEIGHT_ORIENTATION", 0.06)
MEDIA_RANK_WEIGHT_DURATION: float = _get_env_float("MEDIA_RANK_WEIGHT_DURATION", 0.08)
MEDIA_RANK_WEIGHT_GENERIC_STOCK_PENALTY: float = _get_env_float(
    "MEDIA_RANK_WEIGHT_GENERIC_STOCK_PENALTY", 0.10
)
# Soft preference for a candidate whose own tags/URL text match the
# video's locked visual style (see "Visual Style Consistency" section
# below). This is a soft signal only -- the actual hard block on
# mismatched styles happens earlier, via the opposing style's terms
# being merged into the scene's forbidden/negative-keyword lists, so a
# clearly wrong-style candidate is rejected long before it reaches this
# weighted score.
MEDIA_RANK_WEIGHT_STYLE_MATCH: float = _get_env_float("MEDIA_RANK_WEIGHT_STYLE_MATCH", 0.08)
# Soft preference for candidates whose tags suggest professional/
# well-shot footage (see MEDIA_CINEMATIC_TERMS). Deliberately low weight
# -- this is a crude proxy from text tags; the real judgment happens in
# Gemini Vision's own "cinematic_quality" check downstream.
MEDIA_RANK_WEIGHT_CINEMATIC_QUALITY: float = _get_env_float(
    "MEDIA_RANK_WEIGHT_CINEMATIC_QUALITY", 0.04
)

# Generic (non-topic-specific) indicator phrases that flag a candidate
# as subject-less stock filler rather than a real match for the scene.
MEDIA_GENERIC_STOCK_TERMS: List[str] = _get_env_list(
    "MEDIA_GENERIC_STOCK_TERMS",
    [
        "stock footage",
        "stock video",
        "template",
        "abstract background",
        "placeholder",
        "sample clip",
        "loop background",
        "generic background",
    ],
)

# Positive indicator phrases suggesting well-shot, professional footage
# rather than cheap/amateur filler. Used as a soft "cinematic quality"
# ranking signal -- purely a proxy from provider tags/URL text, since
# the real judgment call is made by Gemini Vision (see
# shared.gemini_client.generate_vision_verification's "cinematic_quality"
# check).
MEDIA_CINEMATIC_TERMS: List[str] = _get_env_list(
    "MEDIA_CINEMATIC_TERMS",
    [
        "cinematic",
        "professional",
        "4k",
        "drone",
        "aerial",
        "slow motion",
        "high quality",
        "epic",
        "dramatic lighting",
    ],
)

# ---------------------------------------------------------------------------
# Visual Style Consistency (Stage: Style Lock)
# ---------------------------------------------------------------------------
# Canonical visual-style categories every scene is classified into. This
# is the fixed vocabulary that storyboard_generator's free-text
# "visual_style"/"scene_type" AI output gets canonicalized into, so the
# whole pipeline can reason about "does this candidate's style match the
# video's locked style" with a closed set instead of open-ended text.
MEDIA_STYLE_CATEGORIES: List[str] = _get_env_list(
    "MEDIA_STYLE_CATEGORIES",
    ["Real Footage", "CGI", "3D Render", "2D Illustration", "Vector", "Cartoon"],
)

# Indicator phrases used to (a) canonicalize free-text AI style output
# into one of MEDIA_STYLE_CATEGORIES above, and (b) recognize a
# candidate's own style from its provider tags/URL text. Kept as plain
# data (not code) so new styles/synonyms can be tuned without touching
# any module's logic.
MEDIA_STYLE_TERMS: dict = {
    "Real Footage": [
        "real footage", "live action", "documentary", "real world", "photo",
        "photograph", "camera footage", "handheld", "drone footage", "real life",
    ],
    "CGI": ["cgi", "computer generated", "vfx", "digital effects", "motion graphics"],
    "3D Render": ["3d render", "3d animation", "3d model", "render", "blender", "octane"],
    "2D Illustration": ["2d illustration", "illustration", "flat design", "digital art", "drawing"],
    "Vector": ["vector", "vector art", "flat icon", "infographic", "clipart"],
    "Cartoon": ["cartoon", "animated character", "anime", "toon", "caricature"],
}

# Default style assumed when neither the AI Topic Analyzer nor Scene
# Analyzer produced a recognizable style -- the large majority of stock
# nature/science/history footage used by this pipeline is real-world
# photography/video, so this is the safest silent default.
MEDIA_STYLE_DEFAULT: str = _get_env("MEDIA_STYLE_DEFAULT", "Real Footage") or "Real Footage"

# ---------------------------------------------------------------------------
# Domain Templates
# ---------------------------------------------------------------------------
# A small library of reusable per-domain guardrails, automatically
# matched against each video's AI-derived `scientific_domain` /
# `visual_theme` (see storyboard_generator._match_domain_template) and
# merged (union, never overwrite) into that video's topic_context. This
# is a supplement to the AI's own per-video judgment, not a replacement:
# if a topic doesn't match any template, the pipeline behaves exactly as
# before (pure AI-derived context, no template applied).
DOMAIN_TEMPLATES: dict = {
    "Space": {
        "allowed_objects": ["stars", "planets", "galaxy", "nebula", "astronaut", "rocket", "black hole", "satellite"],
        "forbidden_objects": ["office", "podcast", "kitchen", "beach", "coral reef", "city street", "election", "crowd"],
        "allowed_styles": ["Real Footage", "CGI", "3D Render"],
        "forbidden_styles": ["Vector", "Cartoon", "2D Illustration"],
        "allowed_environment": ["outer space", "observatory", "night sky"],
        "forbidden_environment": ["office", "underwater", "city street"],
    },
    "Ocean": {
        "allowed_objects": ["ocean", "deep sea", "marine life", "coral", "submarine", "waves", "fish", "whale"],
        "forbidden_objects": ["office", "podcast", "galaxy", "outer space", "city street", "election", "crowd"],
        "allowed_styles": ["Real Footage", "3D Render"],
        "forbidden_styles": ["Vector", "Cartoon", "2D Illustration"],
        "allowed_environment": ["underwater", "deep ocean", "beach", "ocean surface"],
        "forbidden_environment": ["outer space", "office", "city street"],
    },
    "Medicine": {
        "allowed_objects": ["doctor", "hospital", "laboratory", "microscope", "cells", "organ", "medical equipment"],
        "forbidden_objects": ["office meeting", "podcast", "outer space", "beach party", "election", "concert"],
        "allowed_styles": ["Real Footage", "3D Render", "CGI"],
        "forbidden_styles": ["Vector", "Cartoon"],
        "allowed_environment": ["hospital", "laboratory", "clinic"],
        "forbidden_environment": ["outer space", "underwater", "concert"],
    },
    "Psychology": {
        "allowed_objects": ["brain", "person thinking", "therapy session", "human face", "silhouette"],
        "forbidden_objects": ["outer space", "coral reef", "election rally", "sports stadium", "podcast studio"],
        "allowed_styles": ["Real Footage", "3D Render", "CGI"],
        "forbidden_styles": ["Vector", "Cartoon"],
        "allowed_environment": ["therapy room", "everyday life", "urban life"],
        "forbidden_environment": ["outer space", "underwater"],
    },
    "History": {
        "allowed_objects": ["historical figures", "old buildings", "artifacts", "battlefield", "archive footage"],
        "forbidden_objects": ["modern smartphone", "outer space", "coral reef", "modern office", "podcast studio"],
        "allowed_styles": ["Real Footage", "2D Illustration"],
        "forbidden_styles": ["Vector", "Cartoon"],
        "allowed_environment": ["historical setting", "museum", "archive"],
        "forbidden_environment": ["modern office", "outer space"],
    },
    "Technology": {
        "allowed_objects": ["computer", "circuit board", "robot", "data center", "smartphone", "code"],
        "forbidden_objects": ["outer space", "coral reef", "election rally", "farm", "forest"],
        "allowed_styles": ["Real Footage", "CGI", "3D Render", "2D Illustration"],
        "forbidden_styles": ["Cartoon"],
        "allowed_environment": ["office", "data center", "lab", "workshop"],
        "forbidden_environment": ["outer space", "underwater", "beach"],
    },
    "Animals": {
        "allowed_objects": ["wildlife", "animal", "habitat", "forest", "savanna", "predator", "prey"],
        "forbidden_objects": ["office", "podcast", "outer space", "election rally", "concert"],
        "allowed_styles": ["Real Footage"],
        "forbidden_styles": ["Vector", "Cartoon", "2D Illustration", "CGI", "3D Render"],
        "allowed_environment": ["forest", "savanna", "jungle", "ocean", "desert"],
        "forbidden_environment": ["office", "outer space", "concert"],
    },
    "Finance": {
        "allowed_objects": ["stock market", "money", "charts", "office", "trading floor", "bank"],
        "forbidden_objects": ["outer space", "coral reef", "jungle", "concert", "farm animals"],
        "allowed_styles": ["Real Footage", "2D Illustration", "Vector"],
        "forbidden_styles": ["Cartoon"],
        "allowed_environment": ["office", "trading floor", "bank", "city skyline"],
        "forbidden_environment": ["outer space", "underwater", "forest"],
    },
}

# ---------------------------------------------------------------------------
# Media Engine -- Fallback / Re-search Loop (Stage 8)
# ---------------------------------------------------------------------------
# How many times the media engine regenerates search queries and
# retries PER PROVIDER before moving on to the next provider in
# MEDIA_PROVIDER_PRIORITY. If every provider is exhausted without a
# candidate clearing MEDIA_MIN_VERIFICATION_SCORE, the scene is left
# without media rather than accepting a weak match.
MEDIA_MAX_SEARCH_ATTEMPTS: int = _get_env_int("MEDIA_MAX_SEARCH_ATTEMPTS", 3)

# ---------------------------------------------------------------------------
# AI Media Verification
# ---------------------------------------------------------------------------
# Minimum overall_score (see shared.gemini_client.generate_vision_verification)
# a candidate must reach to be used. Anything below this is treated as "no
# usable media for this scene" rather than settling for a weak match.
MEDIA_MIN_VERIFICATION_SCORE: float = _get_env_float("MEDIA_MIN_VERIFICATION_SCORE", 0.80)

# ---------------------------------------------------------------------------
# AI Media Verification
# ---------------------------------------------------------------------------
AI_MEDIA_VERIFICATION_MODEL: str = (
    _get_env("AI_MEDIA_VERIFICATION_MODEL", "gemini-vision") or "gemini-vision"
)
AI_MEDIA_VERIFICATION_MIN_SCORE: float = _get_env_float(
    "AI_MEDIA_VERIFICATION_MIN_SCORE", 0.5
)

# ---------------------------------------------------------------------------
# Voice Generator (Edge-TTS)
# ---------------------------------------------------------------------------
VOICE_AUDIO_OUTPUT_DIR: str = _get_env("VOICE_AUDIO_OUTPUT_DIR", "./output/audio") or "./output/audio"
VOICE_CANDIDATES: List[str] = _get_env_list(
    "VOICE_CANDIDATES",
    ["en-US-GuyNeural", "en-US-JennyNeural", "en-GB-RyanNeural", "en-GB-SoniaNeural"],
)
VOICE_SPEED_RANGE: List[str] = _get_env_list("VOICE_SPEED_RANGE", ["-10%", "+0%", "+10%"])
VOICE_PITCH_RANGE: List[str] = _get_env_list("VOICE_PITCH_RANGE", ["-2Hz", "+0Hz", "+2Hz"])
VOICE_NATURAL_PAUSE_MS: int = _get_env_int("VOICE_NATURAL_PAUSE_MS", 250)

# ---------------------------------------------------------------------------
# Subtitle Generator
# ---------------------------------------------------------------------------
SUBTITLE_MAX_WORDS_PER_LINE: int = _get_env_int("SUBTITLE_MAX_WORDS_PER_LINE", 4)
SUBTITLE_HIGHLIGHT_COLOR: str = _get_env("SUBTITLE_HIGHLIGHT_COLOR", "#FFD700") or "#FFD700"

# ---------------------------------------------------------------------------
# Video Composer / Renderer
# ---------------------------------------------------------------------------
VIDEO_WIDTH: int = _get_env_int("VIDEO_WIDTH", 1080)
VIDEO_HEIGHT: int = _get_env_int("VIDEO_HEIGHT", 1920)
VIDEO_FPS: int = _get_env_int("VIDEO_FPS", 30)
VIDEO_OUTPUT_DIR: str = _get_env("VIDEO_OUTPUT_DIR", "./output/video") or "./output/video"
VIDEO_TRANSITIONS: List[str] = _get_env_list(
    "VIDEO_TRANSITIONS", ["fade", "slide", "zoom_blur", "whip_pan"]
)
VIDEO_KEN_BURNS_ENABLED: bool = (_get_env("VIDEO_KEN_BURNS_ENABLED", "true") or "true").lower() == "true"

# ---------------------------------------------------------------------------
# Quality Inspector
# ---------------------------------------------------------------------------
QUALITY_MAX_DURATION_DRIFT_SECONDS: float = _get_env_float(
    "QUALITY_MAX_DURATION_DRIFT_SECONDS", 0.5
)
QUALITY_AUDIO_CLIP_THRESHOLD_DB: float = _get_env_float(
    "QUALITY_AUDIO_CLIP_THRESHOLD_DB", -0.1
)

# ---------------------------------------------------------------------------
# Infrastructure and System Configuration
# ---------------------------------------------------------------------------
DB_BACKEND: str = _get_env("DB_BACKEND", "json") or "json"
ANALYTICS_COLLECTION_INTERVAL_HOURS: int = _get_env_int("ANALYTICS_COLLECTION_INTERVAL_HOURS", 24)
CLEANUP_INTERVAL_HOURS: int = _get_env_int("CLEANUP_INTERVAL_HOURS", 168)
REPORT_OUTPUT_DIR: str = _get_env("REPORT_OUTPUT_DIR", "./output/reports") or "./output/reports"
KNOWLEDGE_DB_DIR: str = _get_env("KNOWLEDGE_DB_DIR", "./db/knowledge") or "./db/knowledge"
SIMILARITY_THRESHOLD: float = _get_env_float("SIMILARITY_THRESHOLD", 0.8)
MONTHLY_UPLOAD_TARGET: int = _get_env_int("MONTHLY_UPLOAD_TARGET", 30)
SCHEDULER_TIMEZONE: str = _get_env("SCHEDULER_TIMEZONE", "UTC") or "UTC"
API_HOST: str = _get_env("API_HOST", "0.0.0.0") or "0.0.0.0"
API_PORT: int = _get_env_int("API_PORT", 8000)
