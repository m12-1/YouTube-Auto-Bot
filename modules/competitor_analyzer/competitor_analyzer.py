"""
modules/competitor_analyzer/competitor_analyzer.py

Analyzes existing competitor content for a given topic using the YouTube Data API.

Public contract:
    run(input_json) -> output_json
"""

from __future__ import annotations

import os
import requests
from typing import Any, Dict, List, Optional
from pydantic import Field

from config import settings
from shared.json_contract import BaseModuleInput, BaseModuleOutput, module_contract, build_response
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)

MODULE_NAME = "competitor_analyzer"

class CompetitorRecord(BaseModuleInput):
    channel_name: str
    video_title: str
    views: int
    engagement_rate: float

class CompetitorInput(BaseModuleInput):
    topic: str

class CompetitorOutput(BaseModuleOutput):
    data: Dict[str, Any] = Field(description="Contains competitors, average_views, saturation_level")

@retry(max_attempts=3, exceptions=(requests.RequestException,))
def _fetch_youtube_competitors(topic: str) -> List[Dict[str, Any]]:
    """
    Fetch real competitor videos from YouTube Data API v3.
    """
    api_key = getattr(settings, "YOUTUBE_SEARCH_API_KEY", None)
    if not api_key:
        logger.warning("YOUTUBE_SEARCH_API_KEY not configured. Returning empty competitors.")
        return []

    search_url = "https://www.googleapis.com/youtube/v3/search"
    search_params = {
        "part": "snippet",
        "q": topic,
        "type": "video",
        "videoDuration": "short",
        "maxResults": 5,
        "key": api_key,
    }
    
    response = requests.get(search_url, params=search_params, timeout=10)
    response.raise_for_status()
    search_data = response.json()
    
    video_ids = [item["id"]["videoId"] for item in search_data.get("items", [])]
    if not video_ids:
        return []
        
    stats_url = "https://www.googleapis.com/youtube/v3/videos"
    stats_params = {
        "part": "statistics,snippet",
        "id": ",".join(video_ids),
        "key": api_key,
    }
    
    stats_resp = requests.get(stats_url, params=stats_params, timeout=10)
    stats_resp.raise_for_status()
    stats_data = stats_resp.json()
    
    competitors = []
    for item in stats_data.get("items", []):
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        
        views = int(statistics.get("viewCount", 0))
        likes = int(statistics.get("likeCount", 0))
        comments = int(statistics.get("commentCount", 0))
        
        engagement_rate = ((likes + comments) / views * 100) if views > 0 else 0.0
        
        competitors.append({
            "channel_name": snippet.get("channelTitle", "Unknown"),
            "video_title": snippet.get("title", "Unknown"),
            "views": views,
            "engagement_rate": round(engagement_rate, 2),
        })
        
    return competitors


def _compute_saturation(average_views: float) -> str:
    """Classify topic saturation based on average competitor views."""
    if average_views < 50_000:
        return "low"
    if average_views < 500_000:
        return "medium"
    return "high"


@module_contract(CompetitorInput, CompetitorOutput, MODULE_NAME)
def run(input_data: CompetitorInput) -> CompetitorOutput:
    """Analyze competitor content for the given topic."""
    competitors = _fetch_youtube_competitors(input_data.topic)
    
    average_views = (
        sum(c["views"] for c in competitors) / len(competitors)
        if competitors
        else 0.0
    )
    saturation_level = _compute_saturation(average_views)

    logger.info(
        f"Competitor analysis complete for run_id={input_data.run_id} topic='{input_data.topic}' "
        f"-> {len(competitors)} competitors, avg_views={average_views:.0f}, saturation={saturation_level}"
    )

    data = {
        "run_id": input_data.run_id,
        "topic": input_data.topic,
        "competitors": competitors,
        "average_views": round(average_views, 2),
        "saturation_level": saturation_level,
    }

    return CompetitorOutput(
        success=True,
        error=None,
        message="Success",
        data=data,
        execution_time=0.0, # populated by decorator
        stage=MODULE_NAME,
        status="success",
        module=MODULE_NAME
    )
