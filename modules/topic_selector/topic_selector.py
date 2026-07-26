"""
modules/topic_selector/topic_selector.py

Selects a content category and a specific topic for this pipeline run.

Generates a topic based on category and past history (to avoid duplicates),
then uses Gemini to refine the topic into a highly engaging concept.

Public contract:
    run(input_json) -> output_json
"""

from __future__ import annotations

import os
import json
import random
from typing import Any, Dict, List, Optional
from pydantic import Field

from config import settings
from shared.json_contract import BaseModuleInput, BaseModuleOutput, module_contract
from shared.logger import get_logger
from shared.path_utils import safe_path, PROJECT_ROOT
from shared.gemini_client import generate_text, GeminiUnavailableError, get_gemini_api_keys

logger = get_logger(__name__)

MODULE_NAME = "topic_selector"

class TopicSelectorInput(BaseModuleInput):
    category_hint: Optional[str] = None

class TopicSelectorOutput(BaseModuleOutput):
    data: Dict[str, Any]


_CATEGORIES = ["science", "history", "technology", "psychology", "finance", "mythology"]


def _get_past_topics() -> List[str]:
    """Read past topics from the database/knowledge base to avoid repetition."""
    past_topics = []
    try:
        db_path = safe_path(PROJECT_ROOT, "db", "knowledge")
        if db_path.exists():
            for file_path in db_path.rglob("*.json"):
                if file_path.is_file():
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if "topic" in data:
                            past_topics.append(data["topic"])
    except Exception as e:
        logger.warning(f"Failed to read past topics: {e}")
    return past_topics


def _generate_base_topic(category: str, past_topics: List[str]) -> str:
    """Generate a base topic concept that hasn't been used recently."""
    base_ideas = {
        "science": ["black holes", "quantum mechanics", "deep ocean", "human body"],
        "history": ["ancient rome", "world war 2 secrets", "lost civilizations", "cold war"],
        "technology": ["AI future", "quantum computers", "internet history", "cybersecurity"],
        "psychology": ["dark psychology", "body language", "placebo effect", "dreams"],
        "finance": ["compound interest", "stock market crashes", "cryptocurrency", "inflation"],
        "mythology": ["greek gods", "norse legends", "egyptian afterlife", "urban legends"]
    }
    
    ideas = base_ideas.get(category, ["general interesting facts"])
    available_ideas = [idea for idea in ideas if idea not in past_topics]
    
    if not available_ideas:
        return random.choice(ideas)
    return random.choice(available_ideas)


def _refine_topic_with_gemini(base_topic: str, category: str) -> str:
    """Use Gemini to refine a basic idea into a highly engaging YouTube Short topic."""
    api_keys = get_gemini_api_keys(settings.GEMINI_KEY_ADVANCED, settings.GEMINI_KEY_FILTER)
    if not api_keys:
        return f"{base_topic.title()} - Explained in 60 Seconds"

    prompt = (
        f"You are an expert YouTube Shorts producer.\n"
        f"Category: {category}\n"
        f"Base Idea: {base_topic}\n\n"
        "Refine this base idea into a highly engaging, click-worthy, and concise topic title "
        "suitable for a 60-second vertical video. Do not include quotes or extra text.\n"
        "Example: 'The Terrifying Reality of Black Holes'"
    )

    try:
        response_text = generate_text(prompt, api_key=api_keys, timeout_seconds=15)
        return response_text.strip().strip('"\'')
    except GeminiUnavailableError as e:
        logger.warning(f"AI topic refinement failed: {e}")
        return f"{base_topic.title()} - Explained in 60 Seconds"


def _extract_keywords(topic: str) -> List[str]:
    """Derive simple search keywords from a topic string."""
    stop_words = {
        "the", "a", "an", "in", "of", "how", "why", "we", "to", "is",
        "actually", "works", "story", "untold", "explained", "seconds", "reality"
    }
    words = [w.strip(".,!?").lower() for w in topic.split()]
    return [w for w in words if w and w not in stop_words][:5]


@module_contract(TopicSelectorInput, TopicSelectorOutput, MODULE_NAME)
def run(input_data: TopicSelectorInput) -> TopicSelectorOutput:
    """Select a category and topic for the given pipeline run."""
    
    category = input_data.category_hint if input_data.category_hint in _CATEGORIES else random.choice(_CATEGORIES)
    if input_data.category_hint and input_data.category_hint not in _CATEGORIES:
        logger.warning(f"category_hint '{input_data.category_hint}' not recognized, falling back to {category}.")

    past_topics = _get_past_topics()
    base_topic = _generate_base_topic(category, past_topics)
    topic = _refine_topic_with_gemini(base_topic, category)
    keywords = _extract_keywords(topic)

    logger.info(
        f"Topic selected for run_id={input_data.run_id} -> category={category} topic='{topic}'"
    )

    data = {
        "run_id": input_data.run_id,
        "category": category,
        "topic": topic,
        "keywords": keywords,
    }

    return TopicSelectorOutput(
        success=True,
        error=None,
        message="Success",
        data=data,
        execution_time=0.0,
        stage=MODULE_NAME,
        status="success",
        module=MODULE_NAME
    )
