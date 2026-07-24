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

_DEFAULT_TEXT_MODEL = "gemini-2.0-flash"
_DEFAULT_VISION_MODEL = "gemini-2.0-flash"
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


def generate_text(
    prompt: str,
    api_key: Optional[str],
    model: str = _DEFAULT_TEXT_MODEL,
    temperature: float = 0.7,
    timeout_seconds: float = 20.0,
) -> str:
    """
    Generate text from a Gemini text model.

    Args:
        prompt: The fully-rendered prompt text to send.
        api_key: A Gemini API key from `config.settings`.
        model: Gemini model name to call.
        temperature: Sampling temperature.
        timeout_seconds: Request timeout, in seconds.

    Returns:
        The generated text.

    Raises:
        GeminiUnavailableError: If `api_key` is missing or the call fails.
    """
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for this call.")

    url = f"{_API_BASE_URL}/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }

    try:
        body = _post(url, payload, timeout_seconds)
        candidates = body.get("candidates", [])
        if not candidates:
            raise GeminiUnavailableError("Gemini returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise GeminiUnavailableError("Gemini returned an empty response.")
        return text
    except GeminiUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini text generation failed: %s", exc)
        raise GeminiUnavailableError(str(exc)) from exc


def generate_vision_score(
    narration_sentence: str,
    image_url: str,
    api_key: Optional[str],
    model: str = _DEFAULT_VISION_MODEL,
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
