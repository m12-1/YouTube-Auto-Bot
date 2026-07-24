"""
Performance Analyzer module.
Analyzes performance of uploaded videos.
"""
from typing import Dict, Any
import os
import json

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
import config.settings
import config.pipeline_config

MODULE_NAME = "performance_analyzer"
logger = get_logger(__name__)

def _load_historical_analytics(video_id: str) -> Dict[str, Any]:
    db_dir = "./db/analytics"
    filepath = os.path.join(db_dir, f"{video_id}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

@retry(max_attempts=3, base_delay_seconds=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyzes performance of uploaded videos.
    """
    logger.info(f"[{MODULE_NAME}] Starting performance analyzer")
    
    try:
        require_keys(input_json, ["run_id"])
        
        video_id = input_json.get("video_id")
        analytics = input_json.get("analytics")
        
        if not analytics and video_id:
            analytics = _load_historical_analytics(video_id)
            
        if not analytics:
            logger.warning(f"[{MODULE_NAME}] No analytics provided or found for {video_id}. Using heuristic fallback.")
            analytics = {
                "views": 100,
                "audience_retention_estimate": 0.5,
                "ctr_estimate": 0.05
            }
            
        retention = float(analytics.get("audience_retention_estimate", 0.0))
        ctr = float(analytics.get("ctr_estimate", 0.0))
        
        # Calculate scores
        retention_score = min(max(retention, 0.0), 1.0)
        ctr_rating = "excellent" if ctr > 0.1 else "good" if ctr > 0.05 else "poor"
        hook_performance = "strong" if retention > 0.6 else "weak"
        
        performance_scores = {
            "retention_score": retention_score,
            "ctr_score": ctr,
            "overall": (retention_score * 0.7) + (ctr * 10 * 0.3)
        }
        
        performance_summary = f"Performance is {ctr_rating} with {hook_performance} hooks."
        recommendations = []
        if hook_performance == "weak":
            recommendations.append("Improve the first 3 seconds to increase hook retention.")
        if ctr_rating == "poor":
            recommendations.append("Test different thumbnails or titles to improve CTR.")
            
        if not recommendations:
            recommendations.append("Continue current strategy, performance is strong.")
            
        return build_response(
            module=MODULE_NAME,
            status="success",
            data={
                "performance_scores": performance_scores,
                "performance_summary": performance_summary,
                "recommendations": recommendations,
                "voice_template_category_performance": "nominal"
            }
        )
            
    except ContractError as e:
        logger.error(f"[{MODULE_NAME}] Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.exception(f"[{MODULE_NAME}] Performance analysis failed: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
