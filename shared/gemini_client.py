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
