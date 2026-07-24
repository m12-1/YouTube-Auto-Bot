"""
modules/fact_verifier/fact_verifier.py

Verifies raw facts produced by the Fact Collector and filters out
anything that fails a confidence threshold.

Uses Gemini to verify fact plausibility against general knowledge.

Public contract:
    run(input_json) -> output_json
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from pydantic import Field

from config import settings
from shared.json_contract import BaseModuleInput, BaseModuleOutput, module_contract
from shared.logger import get_logger
from shared.gemini_client import generate_text, GeminiUnavailableError, pick_api_key

logger = get_logger(__name__)

MODULE_NAME = "fact_verifier"

class RawFact(BaseModuleInput):
    fact_id: str
    text: str
    source_hint: str
    run_id: str = "" # allow passing directly, though not used in verification

class FactVerifierInput(BaseModuleInput):
    topic: str
    raw_facts: List[Dict[str, Any]]
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)

class FactVerifierOutput(BaseModuleOutput):
    data: Dict[str, Any]


def _verify_fact_with_ai(fact: Dict[str, Any], topic: str) -> float:
    """
    Verify a single fact using Gemini.
    """
    api_key = pick_api_key(settings.GEMINI_KEY_ADVANCED, settings.GEMINI_KEY_FILTER)
    if not api_key:
        logger.warning("No Gemini API key available for fact verification. Falling back to confidence 0.5")
        return 0.5

    prompt = (
        f"You are a fact-checker verifying a statement about '{topic}'.\n"
        f"Fact to verify: {fact['text']}\n"
        f"Source context: {fact['source_hint']}\n\n"
        "Analyze the plausibility and correctness of this fact based on general knowledge.\n"
        "Respond ONLY with a JSON object containing a 'confidence' score between 0.0 and 1.0.\n"
        "Example: {\"confidence\": 0.95}"
    )

    try:
        response_text = generate_text(prompt, api_key=api_key)
        cleaned = response_text.strip().strip("`").replace("json\n", "").strip()
        parsed = json.loads(cleaned)
        confidence = float(parsed.get("confidence", 0.5))
        return max(0.0, min(1.0, confidence))
    except (GeminiUnavailableError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to verify fact with AI: {e}")
        return 0.5


def _verify_fact(fact: Dict[str, Any], topic: str, min_confidence: float) -> Dict[str, Any]:
    """Verify a single fact and attach a confidence score."""
    confidence = _verify_fact_with_ai(fact, topic)
    verified = confidence >= min_confidence

    return {
        "fact_id": fact["fact_id"],
        "text": fact["text"],
        "confidence": confidence,
        "verified": verified,
    }


@module_contract(FactVerifierInput, FactVerifierOutput, MODULE_NAME)
def run(input_data: FactVerifierInput) -> FactVerifierOutput:
    """Verify a list of raw facts and filter by confidence threshold."""
    
    scored_facts: List[Dict[str, Any]] = [
        _verify_fact(fact, input_data.topic, input_data.min_confidence) 
        for fact in input_data.raw_facts
    ]

    verified_facts = [f for f in scored_facts if f["verified"]]
    rejected_count = len(scored_facts) - len(verified_facts)

    logger.info(
        f"Fact verification complete for run_id={input_data.run_id} topic='{input_data.topic}' "
        f"-> {len(verified_facts)} verified, {rejected_count} rejected"
    )

    data = {
        "run_id": input_data.run_id,
        "topic": input_data.topic,
        "verified_facts": verified_facts,
        "rejected_count": rejected_count,
    }

    return FactVerifierOutput(
        success=True,
        error=None,
        message="Success",
        data=data,
        execution_time=0.0,
        stage=MODULE_NAME,
        status="success",
        module=MODULE_NAME
    )
