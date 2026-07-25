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

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from config import pipeline_config, settings
from shared.gemini_client import GeminiUnavailableError, generate_text, pick_api_key
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "storyboard_generator"

_STOP_WORDS = {"the", "a", "an", "in", "of", "how", "why", "we", "to", "is", "and", "this", "that"}

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "scene_analyzer_prompt.txt"
_TOPIC_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "topic_analyzer_prompt.txt"

_DEFAULT_TOPIC_CONTEXT = {
    "scientific_domain": "",
    "visual_theme": "",
    "visual_style": "",
    "preferred_environment": "",
    "color_palette": "",
    "lighting_style": "",
    "forbidden_domains": [],
    "forbidden_objects": [],
}

# Generic, always-plausible forbidden settings used as a floor for the
# deterministic (non-AI) fallback, so every scene ships with *some*
# negative keywords even when the AI call is unavailable.
_DEFAULT_FORBIDDEN = ["office", "podcast", "studio interview", "kitchen", "meeting room"]


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


def _load_prompt_template() -> str:
    """Load the Scene Analyzer prompt template from disk."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _analyze_topic_with_ai(topic: str, narration: str) -> Dict[str, Any]:
    """
    Global Topic Understanding (Stage 1): analyze the WHOLE script once,
    before any per-scene analysis, so every scene inherits a consistent
    domain/environment/palette/forbidden list instead of being judged
    in isolation. This is what prevents e.g. "pressure" turning into a
    podcast-microphone shot in an ocean-pressure video.

    Raises:
        GeminiUnavailableError: If no key is configured or the call/parse fails.
    """
    api_key = pick_api_key(
        settings.GEMINI_KEY_ADVANCED,
        settings.GEMINI_KEY_FILTER,
        settings.GEMINI_KEY_FILTER_2,
        settings.GEMINI_KEY_LIGHT,
    )
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for topic analysis.")

    prompt = _TOPIC_PROMPT_PATH.read_text(encoding="utf-8").format(topic=topic, narration=narration)
    text = generate_text(prompt, api_key=api_key, temperature=0.3)

    try:
        cleaned = text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        return {
            "scientific_domain": str(parsed.get("scientific_domain", "")),
            "visual_theme": str(parsed.get("visual_theme", "")),
            "visual_style": str(parsed.get("visual_style", "")),
            "preferred_environment": str(parsed.get("preferred_environment", "")),
            "color_palette": str(parsed.get("color_palette", "")),
            "lighting_style": str(parsed.get("lighting_style", "")),
            "forbidden_domains": [str(d) for d in parsed.get("forbidden_domains", []) if d][:6],
            "forbidden_objects": [str(o) for o in parsed.get("forbidden_objects", []) if o][:8],
        }
    except Exception as exc:  # noqa: BLE001
        raise GeminiUnavailableError(f"Could not parse Topic Analyzer response: {exc}") from exc


def _analyze_topic(topic: str, narration: str) -> Dict[str, Any]:
    """
    Run Global Topic Understanding, preferring AI and falling back to a
    conservative default context (no domain lock, generic forbidden
    list) on any failure so the pipeline still runs without an API key.
    """
    try:
        return _analyze_topic_with_ai(topic, narration)
    except GeminiUnavailableError as exc:
        logger.warning("Topic Analyzer falling back to default context: %s", exc)
        return dict(_DEFAULT_TOPIC_CONTEXT)


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _analyze_scene_with_ai(
    topic: str, narration: str, description: str, topic_context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Ask Gemini to classify a scene: scene_type, environment, required
    objects, forbidden objects, and tiered search keywords. This is the
    "Scene Analyzer" — it understands the *visual meaning* of a
    sentence (e.g. a black-hole metaphor is Outer Space, not a kitchen),
    not just its literal keywords.

    Raises:
        GeminiUnavailableError: If no key is configured or the call/parse fails.
    """
    api_key = pick_api_key(
        settings.GEMINI_KEY_LIGHT,
        settings.GEMINI_KEY_FILTER,
        settings.GEMINI_KEY_FILTER_2,
        settings.GEMINI_KEY_ADVANCED,
    )
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for scene analysis.")

    prompt = _load_prompt_template().format(
        topic=topic,
        narration=narration,
        visual_description=description,
        domain=topic_context.get("scientific_domain", "") or "(not specified)",
        visual_theme=topic_context.get("visual_theme", "") or "(not specified)",
        preferred_environment=topic_context.get("preferred_environment", "") or "(not specified)",
        topic_forbidden_domains=", ".join(topic_context.get("forbidden_domains", [])) or "(none)",
    )
    text = generate_text(prompt, api_key=api_key, temperature=0.3)

    try:
        cleaned = text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)

        return {
            "scene_type": str(parsed.get("scene_type", "Real World Footage")),
            "environment": str(parsed.get("environment", "")),
            "objects": [str(o) for o in parsed.get("objects", []) if o][:6],
            "forbidden": [str(o) for o in parsed.get("forbidden", []) if o][:10],
            "keywords_primary": [str(k).lower() for k in parsed.get("keywords_primary", []) if k][:2],
            "keywords_secondary": [
                str(k).lower() for k in parsed.get("keywords_secondary", []) if k
            ][:5],
            "keywords_negative": [
                str(k).lower() for k in parsed.get("keywords_negative", []) if k
            ][:8],
        }
    except Exception as exc:  # noqa: BLE001
        raise GeminiUnavailableError(f"Could not parse Scene Analyzer response: {exc}") from exc


def _apply_domain_lock(analysis: Dict[str, Any], topic_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Domain Lock (Stage 2), enforced in code so it can't be skipped by a
    prompt the model ignores: hard-merge the video's topic-level
    forbidden domains/objects into every scene's forbidden/negative
    lists, regardless of whether the AI Scene Analyzer remembered to.
    """
    topic_forbidden = list(topic_context.get("forbidden_domains", [])) + list(
        topic_context.get("forbidden_objects", [])
    )
    if not topic_forbidden:
        return analysis

    analysis = dict(analysis)
    analysis["forbidden"] = list(dict.fromkeys(list(analysis.get("forbidden", [])) + topic_forbidden))
    analysis["keywords_negative"] = list(
        dict.fromkeys(
            list(analysis.get("keywords_negative", [])) + [w.lower() for w in topic_forbidden]
        )
    )
    return analysis


def _analyze_scene_heuristic(text: str, topic: str, topic_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic fallback Scene Analyzer, used when no AI key is
    configured or the AI call fails. Cannot infer abstract/metaphorical
    meaning, but still guarantees every scene ships with a non-empty
    forbidden/negative list, and still applies Domain Lock from the
    (possibly AI-derived) topic context.
    """
    primary = _extract_scene_keywords(text, max_keywords=2)
    secondary = _extract_scene_keywords(text, max_keywords=5)[len(primary):]
    if not secondary:
        topic_words = [w for w in topic.lower().split() if len(w) > 3]
        secondary = [w for w in topic_words if w not in primary][:5]

    analysis = {
        "scene_type": "Real World Footage",
        "environment": topic_context.get("preferred_environment", ""),
        "objects": primary or [topic.lower()],
        "forbidden": list(_DEFAULT_FORBIDDEN),
        "keywords_primary": primary or [topic.lower()],
        "keywords_secondary": secondary,
        "keywords_negative": list(_DEFAULT_FORBIDDEN),
    }
    return _apply_domain_lock(analysis, topic_context)


def _analyze_scene(
    narration: str, description: str, topic: str, topic_context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Analyze a scene, preferring the AI Scene Analyzer and falling back
    to the deterministic heuristic on any failure. Domain Lock is
    applied on both paths.
    """
    try:
        analysis = _analyze_scene_with_ai(topic, narration, description, topic_context)
        return _apply_domain_lock(analysis, topic_context)
    except GeminiUnavailableError as exc:
        logger.warning("Scene Analyzer falling back to heuristic: %s", exc)
        return _analyze_scene_heuristic(f"{description} {narration}", topic, topic_context)


def _pick_animation(scene_number: int) -> str:
    """Pick a deterministic animation style for a scene, cycling through options."""
    animations = ["ken_burns", "slide_in", "fade_in", "zoom_pulse"]
    return animations[(scene_number - 1) % len(animations)]


def _pick_transition(scene_number: int) -> str:
    """Pick a deterministic transition style for a scene, cycling through config options."""
    transitions = pipeline_config.VIDEO_TRANSITIONS or ["fade"]
    return transitions[(scene_number - 1) % len(transitions)]


def _build_storyboard(
    scene_breakdown: List[Dict[str, Any]], run_id: str, topic: str, topic_context: Dict[str, Any]
) -> List[Dict[str, Any]]:
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

        analysis = _analyze_scene(narration_excerpt, description, topic, topic_context)

        storyboard.append(
            {
                "scene_id": f"{run_id}-scene-{scene_number}",
                "start_time": start_time,
                "end_time": end_time,
                "narration": narration_excerpt,
                "visual_description": description,
                "keywords": _extract_scene_keywords(f"{description} {narration_excerpt}"),
                "scene_type": analysis["scene_type"],
                "environment": analysis["environment"],
                "objects": analysis["objects"],
                "forbidden": analysis["forbidden"],
                "keywords_primary": analysis["keywords_primary"],
                "keywords_secondary": analysis["keywords_secondary"],
                "keywords_negative": analysis["keywords_negative"],
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

        topic_context = _analyze_topic(topic, script.get("narration", ""))

        storyboard = _build_storyboard(scene_breakdown, run_id, topic, topic_context)
        storyboard = _rescale_to_narration_length(storyboard, script.get("narration", ""))
        total_duration = storyboard[-1]["end_time"] if storyboard else 0.0

        logger.info(
            "Storyboard generated for run_id=%s topic='%s' domain='%s' -> %d scenes, %.2fs total",
            run_id,
            topic,
            topic_context.get("scientific_domain", ""),
            len(storyboard),
            total_duration,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "storyboard": storyboard,
            "total_duration_seconds": total_duration,
            "topic_context": topic_context,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Storyboard Generator contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Storyboard Generator failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
