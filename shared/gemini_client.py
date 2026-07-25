"""
shared/gemini_client.py

Thin, abstract wrapper around Google's Gemini API (text + vision).
Every module that needs an AI call goes through this module instead of
importing an SDK directly, so the underlying model/provider can be
swapped later (per the AI Media Verification requirement to "keep the
Gemini implementation abstract so another model can replace it later")
without touching any calling module.

If no API key is configured, or the call fails for any reason, callers
receive a `GeminiUnavailableError` and are expected to fall back to a
deterministic heuristic — the same pattern already used by
`fact_collector` / `fact_verifier` / `competitor_analyzer` in Part 1.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

_MODEL_FALLBACK_CHAIN: List[str] = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
_DEFAULT_TEXT_MODEL = _MODEL_FALLBACK_CHAIN[0]
_DEFAULT_VISION_MODEL = _MODEL_FALLBACK_CHAIN[0]
_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiUnavailableError(RuntimeError):
    """Raised when the Gemini API cannot be reached or returns an error."""


@retry(max_attempts=2, exceptions=(requests.RequestException,))
def _post(url: str, payload: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
    """
    Perform the raw HTTP POST to the Gemini API.

    Args:
        url: Full request URL, including the API key query parameter.
        payload: JSON request body.
        timeout_seconds: Request timeout, in seconds.

    Returns:
        The parsed JSON response body.

    Raises:
        requests.RequestException: On network-level failures (retried).
    """
    response = requests.post(url, json=payload, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def _generate_text_single_model(
    prompt: str,
    api_key: str,
    model: str,
    temperature: float,
    timeout_seconds: float,
) -> str:
    """
    Call one specific Gemini model. Raises GeminiUnavailableError on any
    failure (network, HTTP error, empty/malformed response) so the caller
    in `generate_text` can decide whether to fall back to the next model
    in the chain.
    """
    url = f"{_API_BASE_URL}/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }

    try:
        body = _post(url, payload, timeout_seconds)
        candidates = body.get("candidates", [])
        if not candidates:
            raise GeminiUnavailableError(f"Gemini model '{model}' returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise GeminiUnavailableError(f"Gemini model '{model}' returned an empty response.")
        return text
    except GeminiUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GeminiUnavailableError(str(exc)) from exc


def generate_text(
    prompt: str,
    api_key: Optional[str],
    model: Optional[str | List[str]] = None,
    temperature: float = 0.7,
    timeout_seconds: float = 20.0,
) -> str:
    """
    Generate text from a Gemini text model, trying a fallback chain of
    lightweight/free-tier models for this same API key if one model is
    unavailable (e.g. rate-limited with 429, retired with 404, or any
    other transient error). Only once every model in the chain has
    failed does this raise `GeminiUnavailableError`, at which point the
    caller falls back to its own deterministic heuristic/template.

    Args:
        prompt: The fully-rendered prompt text to send.
        api_key: A Gemini API key from `config.settings`.
        model: A single model name, a list of model names to try in
            order, or None to use the default fallback chain
            (`_MODEL_FALLBACK_CHAIN`).
        temperature: Sampling temperature.
        timeout_seconds: Request timeout, in seconds.

    Returns:
        The generated text.

    Raises:
        GeminiUnavailableError: If `api_key` is missing, or every model
            in the chain fails.
    """
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for this call.")

    if model is None:
        models_to_try = _MODEL_FALLBACK_CHAIN
    elif isinstance(model, str):
        models_to_try = [model]
    else:
        models_to_try = list(model)

    last_error: Optional[Exception] = None
    for candidate_model in models_to_try:
        try:
            return _generate_text_single_model(
                prompt, api_key, candidate_model, temperature, timeout_seconds
            )
        except GeminiUnavailableError as exc:
            last_error = exc
            logger.warning(
                "Gemini model '%s' unavailable, trying next model in chain: %s",
                candidate_model,
                exc,
            )
            continue

    logger.warning("Gemini text generation failed for all models tried: %s", last_error)
    raise GeminiUnavailableError(
        f"All Gemini models exhausted for this key: {last_error}"
    ) from last_error


def generate_vision_score(
    narration_sentence: str,
    image_url: str,
    api_key: Optional[str],
    model: Optional[str | List[str]] = None,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """
    Score how well a candidate media item matches a narration sentence,
    using a Gemini vision-capable model.

    Args:
        narration_sentence: The sentence this media candidate would
            illustrate.
        image_url: Publicly reachable URL (or thumbnail URL) of the
            candidate media.
        api_key: A Gemini API key from `config.settings`.
        model: Gemini vision-capable model name.
        timeout_seconds: Request timeout, in seconds.

    Returns:
        A dict with "score" (0.0-1.0) and "reason" (str).

    Raises:
        GeminiUnavailableError: If `api_key` is missing or the call fails.
    """
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for this call.")

    prompt = (
        "You are scoring how well a stock video/image matches a narration "
        "sentence for a short-form vertical video. Respond ONLY with JSON: "
        '{"score": <0.0-1.0>, "reason": "<short reason>"}.\n'
        f"Narration sentence: {narration_sentence}\n"
        f"Media URL: {image_url}"
    )

    text = generate_text(prompt, api_key=api_key, model=model, timeout_seconds=timeout_seconds)

    import json

    try:
        cleaned = text.strip().strip("`").replace("json\n", "").strip()
        parsed = json.loads(cleaned)
        return {
            "score": float(parsed.get("score", 0.0)),
            "reason": str(parsed.get("reason", "")),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse Gemini vision response as JSON: %s", exc)
        raise GeminiUnavailableError("Gemini vision response was not valid JSON.") from exc


def generate_vision_verification(
    narration_sentence: str,
    image_url: str,
    api_key: Optional[str],
    required_objects: Optional[List[str]] = None,
    environment: Optional[str] = None,
    forbidden_objects: Optional[List[str]] = None,
    topic: Optional[str] = None,
    scientific_domain: Optional[str] = None,
    previous_scene: Optional[str] = None,
    next_scene: Optional[str] = None,
    required_visual_style: Optional[str] = None,
    forbidden_styles: Optional[List[str]] = None,
    model: Optional[str | List[str]] = None,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """
    Multi-signal verification of a candidate media item — not just "is
    this vaguely related", but whether it matches the sentence, stays
    inside the video's scientific domain, keeps visual continuity with
    the surrounding scenes, doesn't mix visual styles (e.g. a cartoon or
    vector clip inside a Real Footage video), doesn't look like random
    generic stock footage, and is professionally shot. This is what lets
    a black-hole video that scored 8.5/10 become the consistency bar for
    every other topic, instead of score varying 3.5-8.5 depending on how
    literally a word got matched.

    Args:
        required_visual_style: The video's locked canonical visual style
            (one of `pipeline_config.MEDIA_STYLE_CATEGORIES`, e.g.
            "Real Footage"), if known. Optional and backward compatible:
            omitted, the style_match check is skipped (defaults to true)
            exactly like the old behavior.
        forbidden_styles: Style names that must NOT appear (from a
            matched Domain Template), if any.

    Returns:
        {
            "overall_score": float (0.0-1.0),   # weighted combination, gated by contradictions/domain/style
            "score": float,                     # alias of overall_score, for older callers
            "semantic_score": float,            # the 6-question relevance sub-score
            "domain_score": float,              # 1.0 if it stayed in scientific_domain, else 0.0
            "continuity_score": float,          # 1.0 if visually consistent with prev/next scene
            "generic_score": float,             # 1.0 if NOT generic/unrelated stock, else 0.0
            "style_score": float,               # 1.0 if it matches the video's locked visual style, else 0.0
            "cinematic_score": float,           # 1.0 if it looks professionally shot, else 0.0
            "reason": str,
            "checks": {...}
        }

    Raises:
        GeminiUnavailableError: If `api_key` is missing or the call fails.
    """
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for this call.")

    required_str = ", ".join(required_objects) if required_objects else "(not specified)"
    environment_str = environment or "(not specified)"
    forbidden_str = ", ".join(forbidden_objects) if forbidden_objects else "(none specified)"
    domain_str = scientific_domain or "(not specified)"
    topic_str = topic or "(not specified)"
    previous_str = previous_scene or "(this is the first scene)"
    next_str = next_scene or "(this is the last scene)"
    style_str = required_visual_style or "(not specified -- do not penalize style)"
    forbidden_styles_str = ", ".join(forbidden_styles) if forbidden_styles else "(none specified)"

    prompt = (
        "You are a strict media-relevance QA reviewer for a short-form vertical "
        "video. Evaluate the candidate video/image below, considering the WHOLE "
        "video's context, not just this one sentence in isolation.\n\n"
        f"Video topic: {topic_str}\n"
        f"Scientific/subject domain of the whole video: {domain_str}\n"
        f"Required visual style for the WHOLE video (must not be mixed with other "
        f"styles unless the script explicitly calls for it): {style_str}\n"
        f"Forbidden visual styles for this video: {forbidden_styles_str}\n"
        f"Previous scene's narration: {previous_str}\n"
        f"THIS scene's narration: {narration_sentence}\n"
        f"Next scene's narration: {next_str}\n"
        f"Expected environment/setting: {environment_str}\n"
        f"Expected objects/subjects: {required_str}\n"
        f"Forbidden objects/subjects (must NOT be visible): {forbidden_str}\n"
        f"Media URL: {image_url}\n\n"
        "Answer these questions (true/false):\n"
        "1. main_subject_present - is the expected main subject/object visible?\n"
        "2. environment_correct - is the setting/environment correct?\n"
        "3. motion_correct - does any motion/action fit the sentence (or is it "
        "acceptably static)?\n"
        "4. matches_sentence - does the clip overall suit this exact sentence?\n"
        "5. no_contradiction - is it TRUE that nothing forbidden or contradictory "
        "appears (answer false if a forbidden object appears)?\n"
        "6. viewer_would_understand - would a viewer feel this clip explains the "
        "sentence?\n"
        "7. domain_match - does this footage visually belong to the video's "
        "scientific/subject domain above (answer false if it's from a completely "
        "different world, e.g. an office/podcast/city shot in a science video)?\n"
        "8. continuity_ok - does this footage feel like it's from the same visual "
        "world as the previous/next scene narrations (answer true if there's no "
        "jarring, unexplained jump, e.g. deep-ocean footage suddenly cutting to a "
        "galaxy with no script reason)?\n"
        "9. looks_generic_stock - is this obviously generic/unrelated stock "
        "footage that happens to share a keyword but not the meaning?\n"
        "10. style_match - if a required visual style is specified above, does "
        "this clip visually belong to THAT SAME style (answer false if the "
        "required style is real-world footage but this clip is clearly CGI, a 3D "
        "render, a 2D illustration, a vector graphic, or a cartoon, or vice versa; "
        "if no style is specified, answer true)?\n"
        "11. cinematic_quality - does this look like professionally shot, "
        "well-composed, well-lit footage rather than amateur/low-quality filler?\n\n"
        "Respond ONLY with JSON, no markdown fences, in exactly this shape:\n"
        '{"checks": {"main_subject_present": bool, "environment_correct": bool, '
        '"motion_correct": bool, "matches_sentence": bool, "no_contradiction": bool, '
        '"viewer_would_understand": bool, "domain_match": bool, "continuity_ok": bool, '
        '"looks_generic_stock": bool, "style_match": bool, "cinematic_quality": bool}, '
        '"reason": "<one short sentence>"}'
    )

    text = generate_text(prompt, api_key=api_key, model=model, timeout_seconds=timeout_seconds)

    import json

    try:
        cleaned = text.strip().strip("`").replace("json\n", "").strip()
        parsed = json.loads(cleaned)
        bool_keys = (
            "main_subject_present",
            "environment_correct",
            "motion_correct",
            "matches_sentence",
            "no_contradiction",
            "viewer_would_understand",
            "domain_match",
            "continuity_ok",
            "looks_generic_stock",
            "style_match",
            "cinematic_quality",
        )
        raw_checks = parsed.get("checks", {})
        checks = {
            key: bool(raw_checks[key]) if key in raw_checks else (key in ("style_match", "cinematic_quality"))
            for key in bool_keys
        }
        # style_match / cinematic_quality default to True when the model
        # omits them (e.g. an older cached response shape) so this stays
        # backward compatible instead of silently zeroing every score.

        semantic_keys = (
            "main_subject_present",
            "environment_correct",
            "motion_correct",
            "matches_sentence",
            "viewer_would_understand",
        )
        semantic_score = round(sum(1 for k in semantic_keys if checks[k]) / len(semantic_keys), 3)
        domain_score = 1.0 if checks["domain_match"] else 0.0
        continuity_score = 1.0 if checks["continuity_ok"] else 0.5
        generic_score = 0.0 if checks["looks_generic_stock"] else 1.0
        style_score = 1.0 if (checks["style_match"] or not required_visual_style) else 0.0
        cinematic_score = 1.0 if checks["cinematic_quality"] else 0.5

        # Hard gates: a forbidden/contradictory object, a domain break, or
        # (when a style is actually specified) a style mismatch zero the
        # score outright, regardless of how well the rest scores.
        style_violated = bool(required_visual_style) and not checks["style_match"]
        if not checks["no_contradiction"] or not checks["domain_match"] or style_violated:
            overall_score = 0.0
        else:
            overall_score = round(
                0.45 * semantic_score
                + 0.15 * domain_score
                + 0.15 * continuity_score
                + 0.10 * generic_score
                + 0.10 * style_score
                + 0.05 * cinematic_score,
                3,
            )

        return {
            "overall_score": overall_score,
            "score": overall_score,
            "semantic_score": semantic_score,
            "domain_score": domain_score,
            "continuity_score": continuity_score,
            "generic_score": generic_score,
            "style_score": style_score,
            "cinematic_score": cinematic_score,
            "reason": str(parsed.get("reason", "")),
            "checks": checks,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse Gemini verification response as JSON: %s", exc)
        raise GeminiUnavailableError("Gemini verification response was not valid JSON.") from exc


def pick_api_key(*candidates: Optional[str]) -> Optional[str]:
    """
    Pick the first non-empty API key from a prioritized list of
    candidates. Lets callers implement key rotation / fallback across
    e.g. GEMINI_KEY_ADVANCED, GEMINI_KEY_FILTER, GEMINI_KEY_FILTER_2.

    Args:
        *candidates: API key values in priority order.

    Returns:
        The first non-empty key, or None if all are empty.
    """
    for candidate in candidates:
        if candidate:
            return candidate
    return None
