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
# 0.35, not higher: real provider tag/alt-text data rarely echoes the
# exact required_objects / scientific_domain / camera_movement wording
# Scene Analyzer produced (a stock clip tagged "iceberg, arctic, ai
# generated" is a perfectly good underwater match with zero literal
# overlap with "ocean surface, sunlight rays"). Those components then
# legitimately score 0 rather than getting a neutral baseline, since
# the reference list itself isn't empty -- so a strong, correct
# candidate typically lands around 0.40-0.55, not 0.60+. Observed
# production runs with the higher default rejected every candidate on
# every attempt/provider regardless of topic, which is a calibration
# problem, not a signal that the candidates were wrong.
MEDIA_MIN_RANK_SCORE: float = _get_env_float("MEDIA_MIN_RANK_SCORE", 0.35)

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
