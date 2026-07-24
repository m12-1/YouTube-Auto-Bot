"""
modules/fact_collector/fact_collector.py

Collects raw candidate facts for a given topic from Wikipedia.

Public contract:
    run(input_json) -> output_json
"""

from __future__ import annotations

import uuid
import requests
from typing import Any, Dict, List
from pydantic import Field

from shared.json_contract import BaseModuleInput, BaseModuleOutput, module_contract
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "fact_collector"

class FactCollectorInput(BaseModuleInput):
    topic: str
    max_facts: int = Field(default=5, ge=1)

class FactCollectorOutput(BaseModuleOutput):
    data: Dict[str, Any]

@retry(max_attempts=3, exceptions=(requests.RequestException,))
def _collect_raw_facts(topic: str, max_facts: int) -> List[Dict[str, Any]]:
    """
    Produce facts for a topic using Wikipedia API.
    """
    search_url = "https://en.wikipedia.org/w/api.php"
    search_params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": topic,
        "utf8": 1,
    }
    
    response = requests.get(search_url, params=search_params, timeout=10)
    response.raise_for_status()
    search_results = response.json().get("query", {}).get("search", [])
    
    if not search_results:
        return []

    # Get the title of the top result
    top_title = search_results[0]["title"]
    
    extract_params = {
        "action": "query",
        "format": "json",
        "titles": top_title,
        "prop": "extracts",
        "exintro": 1,
        "explaintext": 1,
    }
    
    ext_resp = requests.get(search_url, params=extract_params, timeout=10)
    ext_resp.raise_for_status()
    pages = ext_resp.json().get("query", {}).get("pages", {})
    
    facts: List[Dict[str, Any]] = []
    
    if not pages:
        return facts
        
    for page_id, page_data in pages.items():
        extract = page_data.get("extract", "")
        if not extract:
            continue
            
        # Split into sentences roughly
        sentences = [s.strip() for s in extract.split('.') if len(s.strip()) > 20]
        
        for i, sentence in enumerate(sentences[:max_facts]):
            facts.append(
                {
                    "fact_id": str(uuid.uuid4()),
                    "text": sentence + ".",
                    "source_hint": f"Wikipedia: {top_title}",
                }
            )
            
    return facts

def _fallback_facts(topic: str, max_facts: int) -> List[Dict[str, Any]]:
    """
    Deterministic fallback used whenever Wikipedia is unreachable
    (network error, timeout, 403/404/5xx, rate limit). The pipeline
    must always be able to continue with *something*, even offline.
    """
    generic_angles = [
        "is a topic worth exploring in more detail",
        "has several aspects that are commonly misunderstood",
        "has seen renewed interest in recent discussion",
        "connects to a number of related fields",
        "raises interesting questions for further research",
    ]
    facts: List[Dict[str, Any]] = []
    for i in range(max_facts):
        angle = generic_angles[i % len(generic_angles)]
        facts.append(
            {
                "fact_id": str(uuid.uuid4()),
                "text": f"{topic} {angle}.",
                "source_hint": "fallback: no external source available",
            }
        )
    return facts


@module_contract(FactCollectorInput, FactCollectorOutput, MODULE_NAME)
def run(input_data: FactCollectorInput) -> FactCollectorOutput:
    """Collect raw candidate facts for the given topic."""

    try:
        raw_facts = _collect_raw_facts(input_data.topic, input_data.max_facts)
    except requests.RequestException as exc:
        logger.warning(
            "Wikipedia fact collection failed for run_id=%s topic='%s': %s. "
            "Falling back to placeholder facts.",
            input_data.run_id,
            input_data.topic,
            exc,
        )
        raw_facts = []

    if not raw_facts:
        raw_facts = _fallback_facts(input_data.topic, input_data.max_facts)

    logger.info(
        f"Collected {len(raw_facts)} raw facts for run_id={input_data.run_id} topic='{input_data.topic}'"
    )

    data = {
        "run_id": input_data.run_id,
        "topic": input_data.topic,
        "raw_facts": raw_facts,
    }

    return FactCollectorOutput(
        success=True,
        error=None,
        message="Success",
        data=data,
        execution_time=0.0,
        stage=MODULE_NAME,
        status="success",
        module=MODULE_NAME
    )
