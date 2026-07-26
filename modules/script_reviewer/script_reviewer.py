"""
modules/script_reviewer/script_reviewer.py

Reviews a script produced by the Script Generator for grammar, flow,
viewer retention, simplicity, and duplicated ideas, then returns an
improved version.

Attempts a real Gemini call first; falls back to a deterministic
heuristic pass (de-duplicate near-identical sentences, trim run-on
sentences, collapse double spaces) if no key is configured or the
call fails.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "script": {
            "hook": str, "question": str, "narration": str, "cta": str,
            "scene_breakdown": [...]
        }
    }

output_json:
    {
        "status": "success" | "error",
        "module": "script_reviewer",
        "data": {
            "run_id": str,
            "topic": str,
            "script": { ...same shape as input script... },
            "review_notes": [str, ...],
            "quality_score": float,
            "source": "ai" | "heuristic_fallback"
        },
        "error": str | null
    }
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from config import settings
from shared.gemini_client import GeminiUnavailableError, generate_text, get_gemini_api_keys
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "script_reviewer"

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "prompts",
    "script_reviewer_prompt.txt",
)


def _load_prompt_template() -> str:
    """Load the Script Reviewer prompt template from `prompts/`."""
    with open(_PROMPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _heuristic_review(script: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply a deterministic, rule-based review pass to a script.

    Args:
        script: The original script dict.

    Returns:
        An improved script dict with "review_notes" and "quality_score" added.
    """
    notes: List[str] = []
    narration = script.get("narration", "")

    collapsed = re.sub(r"\s+", " ", narration).strip()
    if collapsed != narration:
        notes.append("Collapsed extra whitespace in narration.")
    narration = collapsed

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", narration) if s.strip()]
    seen = set()
    deduped_sentences: List[str] = []
    for sentence in sentences:
        key = sentence.lower()
        if key in seen:
            notes.append(f"Removed duplicated sentence: '{sentence[:40]}...'")
            continue
        seen.add(key)
        deduped_sentences.append(sentence)

    narration = " ".join(deduped_sentences)

    long_sentences = [s for s in deduped_sentences if len(s.split()) > 30]
    if long_sentences:
        notes.append(
            f"Flagged {len(long_sentences)} run-on sentence(s) over 30 words for simplification."
        )

    if not notes:
        notes.append("No issues found; script passed heuristic review as-is.")

    quality_score = max(0.5, 1.0 - 0.05 * len(long_sentences) - 0.02 * (len(sentences) - len(deduped_sentences)))
    quality_score = round(min(quality_score, 1.0), 3)

    improved = dict(script)
    improved["narration"] = narration
    improved["review_notes"] = notes
    improved["quality_score"] = quality_score
    return improved


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _review_with_ai(script: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attempt to review the script using the Gemini text API.

    Args:
        script: The original script dict.

    Returns:
        An improved script dict with "review_notes" and "quality_score".

    Raises:
        GeminiUnavailableError: If no key is configured or the call fails.
    """
    api_keys = get_gemini_api_keys(settings.GEMINI_KEY_ADVANCED, settings.GEMINI_KEY_LIGHT)
    if not api_keys:
        raise GeminiUnavailableError("No Gemini API key configured for script_reviewer.")

    template = _load_prompt_template()
    prompt = template.format(script_json=json.dumps(script, ensure_ascii=False, indent=2))

    raw_text = generate_text(prompt, api_key=api_keys)
    cleaned = raw_text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # The model occasionally emits malformed JSON (e.g. an unescaped
        # apostrophe/quote inside a string value). Treat this the same as
        # an unavailable API response so the caller falls back to the
        # deterministic heuristic reviewer instead of crashing the stage.
        raise GeminiUnavailableError(f"AI review response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GeminiUnavailableError("AI review response was not a JSON object.")

    for key in ("hook", "question", "narration", "cta", "scene_breakdown", "review_notes", "quality_score"):
        if key not in parsed:
            raise GeminiUnavailableError(f"AI review response missing key '{key}'.")

    return parsed


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Review and improve a generated script.

    Args:
        input_json: Must contain "run_id", "topic", and "script".

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

        source = "ai"
        try:
            reviewed = _review_with_ai(script)
        except GeminiUnavailableError as exc:
            logger.warning(
                "script_reviewer falling back to heuristic review for run_id=%s: %s",
                run_id,
                exc,
            )
            reviewed = _heuristic_review(script)
            source = "heuristic_fallback"

        review_notes = reviewed.pop("review_notes", [])
        quality_score = reviewed.pop("quality_score", 0.0)

        logger.info(
            "Script reviewed for run_id=%s topic='%s' source=%s quality_score=%.3f",
            run_id,
            topic,
            source,
            quality_score,
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "script": reviewed,
            "review_notes": review_notes,
            "quality_score": quality_score,
            "source": source,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Script Reviewer contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Script Reviewer failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
