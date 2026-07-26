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

# Wikipedia's API rejects requests with no identifying User-Agent (403 Forbidden).
# See: https://meta.wikimedia.org/wiki/User-Agent_policy
WIKIPEDIA_HEADERS = {
    "User-Agent": "YouTube-Auto-Bot/1.0 (https://github.com/Mohammed-163/YouTube-Auto-Bot; contact: bot-maintainer@example.com) requests/python"
}

class FactCollectorInput(BaseModuleInput):
    topic: str
    max_facts: int = Field(default=5, ge=1)

class FactCollectorOutput(BaseModuleOutput):
    data: Dict[str, Any]

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "in", "on", "at",
    "to", "for", "and", "or", "but", "why", "how", "what", "when", "where",
    "does", "do", "did", "you", "your", "it", "its", "this", "that", "with",
    "until", "look", "exist", "reality", "not", "no", "yes", "can", "will",
}


def _topic_keywords(topic: str) -> set:
    """Significant (non-stopword, length>=4) words from the topic string."""
    words = [w.strip(".,!?'\"").lower() for w in topic.split()]
    return {w for w in words if len(w) >= 4 and w not in _STOPWORDS}


def _is_relevant_title(title: str, topic_keywords: set) -> bool:
    """
    True if a Wikipedia page title shares at least one significant word
    with the topic. This exists specifically to catch cases like topic
    "Why Reality Doesn't Exist Until You Look" (a quantum-physics/
    observer-effect topic) where Wikipedia's full-text search engine can
    rank an unrelated page -- e.g. a "reality" TV show or celebrity
    biography -- as the #1 hit purely because it contains the word
    "reality", even though it has nothing to do with the actual topic.
    Blindly using search_results[0] without this check is what let an
    entirely off-topic page (and its facts) flow straight into the
    script, causing the LLM to write about it as if it were relevant.
    """
    if not topic_keywords:
        return True
    title_words = {w.strip(".,!?'\"").lower() for w in title.split()}
    return bool(title_words & topic_keywords)


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
        "srlimit": 5,
        "utf8": 1,
    }
    
    response = requests.get(
        search_url, params=search_params, headers=WIKIPEDIA_HEADERS, timeout=10
    )
    response.raise_for_status()
    search_results = response.json().get("query", {}).get("search", [])
    
    if not search_results:
        return []

    # Walk the top results (not just [0]) and pick the FIRST one whose
    # title is actually relevant to the topic. If none of them are
    # relevant, bail out to the deterministic fallback rather than risk
    # feeding an off-topic Wikipedia page into the script.
    topic_keywords = _topic_keywords(topic)
    top_title = None
    for result in search_results:
        candidate_title = result.get("title", "")
        if _is_relevant_title(candidate_title, topic_keywords):
            top_title = candidate_title
            break

    if top_title is None:
        logger.warning(
            "No Wikipedia search result for topic='%s' looked topically relevant "
            "(top candidates: %s); skipping Wikipedia and using fallback facts "
            "instead of risking an off-topic page.",
            topic,
            [r.get("title") for r in search_results],
        )
        return []
    
    extract_params = {
        "action": "query",
        "format": "json",
        "titles": top_title,
        "prop": "extracts",
        "exintro": 1,
        "explaintext": 1,
    }
    
    ext_resp = requests.get(
        search_url, params=extract_params, headers=WIKIPEDIA_HEADERS, timeout=10
    )
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
