"""
modules/seo_generator/seo_generator.py

Generates YouTube Shorts / TikTok SEO metadata (title, description,
tags, hashtags, CTR prediction, SEO score) from the topic, the
competitor analysis, and the final reviewed script's narration.

Attempts a real Gemini call first (via `shared.gemini_client`, using
`config.settings.GEMINI_KEY_ADVANCED`, falling back to
`GEMINI_KEY_LIGHT`). If no key is configured or the call fails, falls
back to a deterministic template-based SEO package so the pipeline
never stalls on a missing/expired credential.

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

input_json (optional keys):
    {
        "competitors": [
            {"channel_name": str, "video_title": str, "views": int,
             "engagement_rate": float},
            ...
        ],
        "average_views": int,
        "saturation_level": str
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
            "source": "ai" | "template_fallback"
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

_REQUIRED_SEO_KEYS = (
    "title",
    "description",
    "tags",
    "hashtags",
    "ctr_prediction",
    "seo_score",
)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "is", "are", "was", "were", "it", "its", "this", "that", "with",
    "as", "at", "by", "be", "been", "has", "have", "had", "so", "did",
    "you", "your", "we", "our", "wait", "really", "going",
}


def _load_prompt_template() -> str:
    """
    Load the SEO Generator prompt template from `prompts/`.

    Returns:
        The raw prompt template text.
    """
    with open(_PROMPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _format_competitor_analysis(input_json: Dict[str, Any]) -> str:
    """
    Build a short competitor-analysis text block for the prompt from
    whatever `competitor_analyzer` output is available on the payload.

    Args:
        input_json: The accumulated pipeline payload.

    Returns:
        A human-readable competitor analysis summary, or a neutral
        placeholder if no competitor data was supplied.
    """
    competitors = input_json.get("competitors") or []
    average_views = input_json.get("average_views")
    saturation_level = input_json.get("saturation_level")

    if not competitors and average_views is None and saturation_level is None:
        return "(no competitor analysis available)"

    lines: List[str] = []
    if saturation_level is not None:
        lines.append(f"Market saturation: {saturation_level}")
    if average_views is not None:
        lines.append(f"Average competitor views: {average_views}")
    for competitor in competitors[:5]:
        lines.append(
            f"- '{competitor.get('video_title', 'Unknown title')}' by "
            f"{competitor.get('channel_name', 'unknown channel')} "
            f"({competitor.get('views', 0)} views, "
            f"{competitor.get('engagement_rate', 0)} engagement rate)"
        )
    return "\n".join(lines)


def _extract_keywords(topic: str, narration: str, limit: int) -> List[str]:
    """
    Deterministically extract simple keyword candidates from the topic
    and narration for the template fallback path.

    Args:
        topic: The video topic.
        narration: The final narration text.
        limit: Maximum number of keywords to return.

    Returns:
        A list of lowercase keyword strings, topic words first.
    """
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", f"{topic} {narration}".lower())
    seen: List[str] = []
    for word in words:
        if word in _STOPWORDS or word in seen:
            continue
        seen.append(word)
        if len(seen) >= limit:
            break
    return seen or [topic.lower()]


def _build_template_seo(topic: str, script: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministically build a fallback SEO package from a topic and its
    script, without calling any AI model.

    Args:
        topic: The video topic.
        script: The reviewed script dict (needs "narration").

    Returns:
        A seo dict matching the module's output shape.
    """
    narration = script.get("narration", "") if isinstance(script, dict) else ""

    title = f"You Won't Believe This About {topic}"[: pipeline_config.SEO_TITLE_MAX_LENGTH]

    description = (
        f"Discover a fascinating fact about {topic} in this short video. "
        f"We break down what most people get wrong about {topic} and why it matters."
    )

    keywords = _extract_keywords(topic, narration, pipeline_config.SEO_MAX_TAGS)
    tags = keywords[: pipeline_config.SEO_MAX_TAGS]
    hashtags = [f"#{word}" for word in keywords[: pipeline_config.SEO_MAX_HASHTAGS]]
    if not hashtags:
        hashtags = ["#shorts"]

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "ctr_prediction": 5.0,
        "seo_score": 0.5,
    }


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _generate_with_ai(topic: str, competitor_analysis: str, narration: str) -> Dict[str, Any]:
    """
    Attempt to generate an SEO package using the Gemini text API.

    Args:
        topic: The video topic.
        competitor_analysis: Formatted competitor analysis text block.
        narration: The final narration text.

    Returns:
        A seo dict matching the module's output shape.

    Raises:
        GeminiUnavailableError: If no key is configured or the call fails.
    """
    api_key = pick_api_key(settings.GEMINI_KEY_ADVANCED, settings.GEMINI_KEY_LIGHT)
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for seo_generator.")

    template = _load_prompt_template()
    prompt = template.format(
        topic=topic,
        competitor_analysis=competitor_analysis or "(no competitor analysis available)",
        narration=narration or "(no narration available)",
        title_max_length=pipeline_config.SEO_TITLE_MAX_LENGTH,
        max_tags=pipeline_config.SEO_MAX_TAGS,
        max_hashtags=pipeline_config.SEO_MAX_HASHTAGS,
    )

    raw_text = generate_text(prompt, api_key=api_key)
    cleaned = raw_text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiUnavailableError(f"AI SEO response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GeminiUnavailableError("AI SEO response was not a JSON object.")

    for key in _REQUIRED_SEO_KEYS:
        if key not in parsed:
            raise GeminiUnavailableError(f"AI SEO response missing key '{key}'.")

    return parsed


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate SEO metadata (title, description, tags, hashtags, CTR
    prediction, SEO score) for the given topic and script.

    Args:
        input_json: Must contain "run_id", "topic", and "script". May
            contain "competitors", "average_views", "saturation_level".

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

        narration = script.get("narration", "")
        competitor_analysis = _format_competitor_analysis(input_json)

        source = "ai"
        try:
            seo = _generate_with_ai(topic, competitor_analysis, narration)
        except GeminiUnavailableError as exc:
            logger.warning(
                "seo_generator falling back to template for run_id=%s: %s",
                run_id,
                exc,
            )
            seo = _build_template_seo(topic, script)
            source = "template_fallback"

        logger.info(
            "SEO generated for run_id=%s topic='%s' source=%s (%d tags, %d hashtags)",
            run_id,
            topic,
            source,
            len(seo.get("tags", [])),
            len(seo.get("hashtags", [])),
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
