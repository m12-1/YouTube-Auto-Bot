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
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
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
    model: Optional[str | List[str]] = None,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """
    Multi-criteria verification of a candidate media item against a
    narration sentence and its Scene Analyzer requirements. Unlike
    `generate_vision_score` (one generic relevance question), this asks
    several independent yes/no questions and derives a weighted score,
    so a clip can't pass just because it's "vaguely related".

    Returns:
        {
            "score": float (0.0-1.0),
            "reason": str,
            "checks": {
                "main_subject_present": bool, "environment_correct": bool,
                "motion_correct": bool, "matches_sentence": bool,
                "no_contradiction": bool, "viewer_would_understand": bool
            }
        }

    Raises:
        GeminiUnavailableError: If `api_key` is missing or the call fails.
    """
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for this call.")

    required_str = ", ".join(required_objects) if required_objects else "(not specified)"
    environment_str = environment or "(not specified)"
    forbidden_str = ", ".join(forbidden_objects) if forbidden_objects else "(none specified)"

    prompt = (
        "You are a strict media-relevance QA reviewer for a short-form vertical "
        "video. Evaluate the candidate video/image against the narration sentence "
        "below by answering SIX independent yes/no questions.\n\n"
        f"Narration sentence: {narration_sentence}\n"
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
        "sentence?\n\n"
        "Respond ONLY with JSON, no markdown fences, in exactly this shape:\n"
        '{"checks": {"main_subject_present": bool, "environment_correct": bool, '
        '"motion_correct": bool, "matches_sentence": bool, "no_contradiction": bool, '
        '"viewer_would_understand": bool}, "reason": "<one short sentence>"}'
    )

    text = generate_text(prompt, api_key=api_key, model=model, timeout_seconds=timeout_seconds)

    import json

    try:
        cleaned = text.strip().strip("`").replace("json\n", "").strip()
        parsed = json.loads(cleaned)
        checks = {
            key: bool(parsed.get("checks", {}).get(key, False))
            for key in (
                "main_subject_present",
                "environment_correct",
                "motion_correct",
                "matches_sentence",
                "no_contradiction",
                "viewer_would_understand",
            )
        }
        # no_contradiction is a hard gate: any forbidden/contradictory object
        # visible caps the score regardless of the other five answers.
        if not checks["no_contradiction"]:
            score = 0.0
        else:
            passed = sum(1 for value in checks.values() if value)
            score = round(passed / len(checks), 3)

        return {"score": score, "reason": str(parsed.get("reason", "")), "checks": checks}
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
