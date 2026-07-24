"""
Analytics Collector module.
Collects YouTube analytics periodically.
"""
from typing import Dict, Any
import datetime
import os
import json

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
import config.settings
import config.pipeline_config

MODULE_NAME = "analytics_collector"
logger = get_logger(__name__)

try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    HAS_GOOGLE_API = True
except ImportError:
    HAS_GOOGLE_API = False

def _save_analytics(video_id: str, data: Dict[str, Any]) -> None:
    db_dir = "./db/analytics"
    os.makedirs(db_dir, exist_ok=True)
    filepath = os.path.join(db_dir, f"{video_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

@retry(max_attempts=3, base_delay_seconds=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Collects YouTube analytics for videos.
    """
    logger.info(f"[{MODULE_NAME}] Starting analytics collection")
    
    try:
        require_keys(input_json, ["run_id"])
        
        run_id = input_json.get("run_id")
        video_id = input_json.get("video_id")
        video_ids = input_json.get("video_ids", [])
        if video_id and video_id not in video_ids:
            video_ids.append(video_id)
            
        collection_period = input_json.get("collection_period", "daily")
        
        client_id = getattr(config.settings, "YOUTUBE_OAUTH_CLIENT_ID", None)
        client_secret = getattr(config.settings, "YOUTUBE_OAUTH_CLIENT_SECRET", None)
        refresh_token = getattr(config.settings, "YOUTUBE_OAUTH_REFRESH_TOKEN", None)
        
        if not video_ids:
            logger.info(f"[{MODULE_NAME}] No video IDs provided for analytics.")
            return build_response(MODULE_NAME, "success", data={"analytics": [], "collection_timestamp": datetime.datetime.now().isoformat()})
            
        if not HAS_GOOGLE_API or not all([client_id, client_secret, refresh_token]):
            logger.warning(f"[{MODULE_NAME}] API client or credentials missing. Falling back to simulated analytics.")
            simulated_results = []
            for vid in video_ids:
                data = {
                    "video_id": vid,
                    "views": 1500,
                    "watch_time_minutes": 25.0,
                    "average_view_duration": 45,
                    "audience_retention_estimate": 0.8,
                    "ctr_estimate": 0.12,
                    "subscribers_gained": 10,
                    "comments": 5,
                    "likes": 150,
                    "traffic_sources_estimate": {"shorts_feed": 0.9, "search": 0.1}
                }
                _save_analytics(vid, data)
                simulated_results.append(data)
                
            return build_response(
                module=MODULE_NAME,
                status="success",
                data={
                    "analytics": simulated_results,
                    "collection_timestamp": datetime.datetime.now().isoformat(),
                    "source": "simulated_fallback"
                }
            )
            
        credentials = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        
        youtube = build("youtube", "v3", credentials=credentials)
        results = []
        
        for vid in video_ids:
            logger.info(f"[{MODULE_NAME}] Fetching analytics for {vid}")
            request = youtube.videos().list(
                part="statistics",
                id=vid
            )
            response = request.execute()
            items = response.get("items", [])
            
            if items:
                stats = items[0].get("statistics", {})
                data = {
                    "video_id": vid,
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "watch_time_minutes": 0.0,
                    "average_view_duration": 0,
                    "audience_retention_estimate": 0.0,
                    "ctr_estimate": 0.0,
                    "subscribers_gained": 0,
                    "traffic_sources_estimate": {}
                }
                _save_analytics(vid, data)
                results.append(data)
            else:
                logger.warning(f"[{MODULE_NAME}] Video {vid} not found.")
                
        return build_response(
            module=MODULE_NAME,
            status="success",
            data={
                "analytics": results,
                "collection_timestamp": datetime.datetime.now().isoformat(),
                "source": "youtube_api"
            }
        )
            
    except ContractError as e:
        logger.error(f"[{MODULE_NAME}] Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.exception(f"[{MODULE_NAME}] Analytics collection failed: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
