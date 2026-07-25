"""
modules/script_generator/script_generator.py

Generates a short-form video script (hook, question, narration, CTA,
scene breakdown) from a topic and its verified facts.

Attempts a real Gemini call first (via `shared.gemini_client`, using
`config.settings.GEMINI_KEY_ADVANCED`). If no key is configured or the
call fails, falls back to a deterministic template-based script so the
pipeline never stalls on a missing/expired credential.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "verified_facts": [
            {"fact_id": str, "text": str, "confidence": float, "verified": bool},
            ...
        ]
    }

input_json (optional keys):
    {
        "category": str
    }

output_json:
    {
        "status": "success" | "error",
        "module": "script_generator",
        "data": {
            "run_id": str,
            "topic": str,
            "script": {
                "hook": str,
                "question": str,
                "narration": str,
                "cta": str,
                "scene_breakdown": [
                    {"scene_number": int, "description": str, "narration_excerpt": str},
                    ...
                ]
            },
            "source": "ai" | "template_fallback"
        },
        "error": str | null
    }
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from config import pipeline_config, settings
from shared.gemini_client import GeminiUnavailableError, generate_text, pick_api_key
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "script_generator"

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "prompts",
    "script_generator_prompt.txt",
)


def _load_prompt_template() -> str:
    """
    Load the Script Generator prompt template from `prompts/`.

    Returns:
        The raw prompt template text.
    """
    with open(_PROMPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _build_template_script(topic: str, verified_facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Deterministically build a fallback script from a topic and its
    verified facts, without calling any AI model.

    Args:
        topic: The video topic.
        verified_facts: List of verified fact dicts.

    Returns:
        A script dict matching the module's output shape.
    """
    fact_texts = [f["text"] for f in verified_facts] or [
        f"{topic} has more layers than most people realize."
    ]

    hook = f"Wait — did you know this about {topic}?"
    question = f"So what's really going on with {topic}?"
    narration_sentences = [hook] + fact_texts + [
        f"That's the real story behind {topic}."
    ]
    narration = " ".join(narration_sentences)
    cta = "Follow for more facts like this."

    scene_breakdown = []
    for i, sentence in enumerate(narration_sentences, start=1):
        scene_breakdown.append(
            {
                "scene_number": i,
                "description": f"Visual illustrating: {sentence[:60]}",
                "narration_excerpt": sentence,
            }
        )

    return {
        "hook": hook,
        "question": question,
        "narration": narration,
        "cta": cta,
        "scene_breakdown": scene_breakdown,
    }


@retry(max_attempts=2, exceptions=(GeminiUnavailableError,))
def _generate_with_ai(
    topic: str, category: str, verified_facts: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Attempt to generate a script using the Gemini text API.

    Args:
        topic: The video topic.
        category: The content category.
        verified_facts: List of verified fact dicts.

    Returns:
        A script dict matching the module's output shape.

    Raises:
        GeminiUnavailableError: If no key is configured or the call fails.
    """
    api_key = pick_api_key(settings.GEMINI_KEY_ADVANCED, settings.GEMINI_KEY_LIGHT)
    if not api_key:
        raise GeminiUnavailableError("No Gemini API key configured for script_generator.")

    template = _load_prompt_template()
    facts_block = "\n".join(f"- {f['text']}" for f in verified_facts)
    prompt = template.format(
        topic=topic,
        category=category,
        verified_facts=facts_block or "- (no verified facts supplied)",
        target_word_count=pipeline_config.SCRIPT_TARGET_WORD_COUNT,
    )

    raw_text = generate_text(prompt, api_key=api_key)
    cleaned = raw_text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiUnavailableError(f"AI script response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GeminiUnavailableError("AI script response was not a JSON object.")

    for key in ("hook", "question", "narration", "cta", "scene_breakdown"):
        if key not in parsed:
            raise GeminiUnavailableError(f"AI script response missing key '{key}'.")

    return parsed


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a short-form video script for the given topic.

    Args:
        input_json: Must contain "run_id", "topic", and "verified_facts".
            May contain "category".

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "verified_facts"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        category = input_json.get("category", "general")
        verified_facts = input_json["verified_facts"]

        if not isinstance(verified_facts, list):
            raise ContractError("verified_facts must be a list")

        source = "ai"
        try:
            script = _generate_with_ai(topic, category, verified_facts)
        except GeminiUnavailableError as exc:
            logger.warning(
                "script_generator falling back to template for run_id=%s: %s",
                run_id,
                exc,
            )
            script = _build_template_script(topic, verified_facts)
            source = "template_fallback"

        logger.info(
            "Script generated for run_id=%s topic='%s' source=%s (%d scenes)",
            run_id,
            topic,
            source,
            len(script.get("scene_breakdown", [])),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "script": script,
            "source": source,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Script Generator contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Script Generator failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
