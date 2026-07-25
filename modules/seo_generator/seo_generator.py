"""
modules/seo_generator/seo_generator.py

Generates SEO metadata (title, description, tags, hashtags, CTR
prediction, SEO score) from a topic, competitor analysis, and the
final reviewed script.

Attempts a real Gemini call first; falls back to a deterministic
keyword-based heuristic if no key is configured or the call fails.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "script": {"narration": str, ...}
    }

input_json (optional keys):
    {
        "competitors": [ {"channel_name": str, "video_title": str, ...}, ... ],
        "average_views": float
    }

output_json:
    {
        "status": "success" | "error",
        "module": "seo_generator",
        "data": {
            "run_id": str,
            "topic": str,
            "seo": {
                "title": str,
                "description": str,
                "tags": [str, ...],
                "hashtags": [str, ...],
                "ctr_prediction": float,
                "seo_score": float
            },
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

from config import pipeline_config, settings
from shared.gemini_client import GeminiUnavailableError, generate_text, pick_api_key
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "seo_generator"

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "prompts",
    "seo_generator_prompt.txt",
)

_STOP_WORDS = {
    "the", "a", "an", "in", "of", "how", "why", "we", "to", "is", "and",
    "that", "this", "for", "on", "with", "it", "as", "at", "by",
}


def _load_prompt_template() -> str:
    """Load the SEO Generator prompt template from `prompts/`."""
    with open(_PROMPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _extract_keywords(topic: str, narration: str, max_keywords: int) -> List[str]:
    """
    Derive simple keyword tags from the topic and narration text.

    Args:
        topic: The video topic.
        narration: The final narration text.
        max_keywords: Maximum number of keywords to return.

    Returns:
        A deduplicated list of lowercase keyword strings.
    """
    words = re.findall(r"[A-Za-z']+", f"{topic} {narration}".lower())
    seen: List[str] = []
    for word in words:
        if word in _STOP_WORDS or len(word) < 3:
            continue
        if word not in seen:
            seen.append(word)
        if len(seen) >= max_keywords:
            break
    return seen


def _build_heuristic_seo(
    topic: str,
    narration: str,
    average_views: float,
    saturation_level: str,
) -> Dict[str, Any]:
    """
    Deterministically build SEO metadata without calling any AI model.

    Args:
        topic: The video topic.
        narration: The final narration text.
        average_views: Average competitor view count (0 if unknown).
        saturation_level: Competitor saturation ("low"/"medium"/"high").

    Returns:
        An SEO metadata dict matching the module's output shape.
    """
    title = f"You Won't Believe This About {topic}"[: pipeline_config.SEO_TITLE_MAX_LENGTH]
    description = (
        f"A quick, fact-checked breakdown of {topic}. "
        f"{narration[:180]}..."
        if len(narration) > 180
        else f"A quick, fact-checked breakdown of {topic}. {narration}"
    )

    tags = _extract_keywords(topic, narration, pipeline_config.SEO_MAX_TAGS)
    hashtags = [f"#{t}" for t in tags[: pipeline_config.SEO_MAX_HASHTAGS]]

    saturation_penalty = {"low": 0.0, "medium": 0.15, "high": 0.3}.get(saturation_level, 0.15)
    ctr_prediction = round(max(2.0, 9.0 - saturation_penalty * 10), 2)
    seo_score = round(max(0.4, 0.9 - saturation_penalty), 3)

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "ctr_prediction": ctr_prediction,
        "seo_score": seo_score,
    }


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _generate_with_ai(
    topic: str, competitors: List[Dict[str, Any]], narration: str
) -> Dict[str, Any]:
    """
    Attempt to generate SEO metadata using the Gemini text API.

    Args:
        topic: The video topic.
        competitors: List of competitor records.
        narration: The final narration text.

    Returns:
        An SEO metadata dict matching the module's output shape.

    Raises:
        GeminiUnavailableError: If no key is configured or the call fails.
    """
    api_key = pick_api_key(
        settings.GEMINI_KEY_FILTER, settings.GEMINI_KEY_FILTER_2, settings.GEMINI_KEY_LIGHT
    )
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for seo_generator.")

    template = _load_prompt_template()
    competitor_block = "\n".join(
        f"- {c.get('channel_name', 'unknown')}: {c.get('video_title', '')}"
        for c in competitors
    ) or "- (no competitor data supplied)"

    prompt = template.format(
        topic=topic,
        competitor_analysis=competitor_block,
        narration=narration,
        title_max_length=pipeline_config.SEO_TITLE_MAX_LENGTH,
        max_tags=pipeline_config.SEO_MAX_TAGS,
        max_hashtags=pipeline_config.SEO_MAX_HASHTAGS,
    )

    raw_text = generate_text(prompt, api_key=api_key)
    cleaned = raw_text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    parsed = json.loads(cleaned)
    for key in ("title", "description", "tags", "hashtags", "ctr_prediction", "seo_score"):
        if key not in parsed:
            raise GeminiUnavailableError(f"AI SEO response missing key '{key}'.")

    return parsed


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate SEO metadata for the given topic and script.

    Args:
        input_json: Must contain "run_id", "topic", and "script".
            May contain "competitors" and "average_views".

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "script"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        script = input_json["script"]
        competitors = input_json.get("competitors", [])
        saturation_level = input_json.get("saturation_level", "medium")
        average_views = float(input_json.get("average_views", 0.0))

        if not isinstance(script, dict):
            raise ContractError("script must be a dict")

        narration = script.get("narration", "")

        source = "ai"
        try:
            seo = _generate_with_ai(topic, competitors, narration)
        except GeminiUnavailableError as exc:
            logger.warning(
                "seo_generator falling back to heuristic for run_id=%s: %s", run_id, exc
            )
            seo = _build_heuristic_seo(topic, narration, average_views, saturation_level)
            source = "heuristic_fallback"

        logger.info(
            "SEO generated for run_id=%s topic='%s' source=%s seo_score=%.3f",
            run_id,
            topic,
            source,
            float(seo.get("seo_score", 0.0)),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "seo": seo,
            "source": source,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("SEO Generator contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("SEO Generator failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
