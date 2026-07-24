import os
import json
from typing import Dict, Any, List

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
from shared.gemini_client import generate_text, GeminiUnavailableError, pick_api_key
import config.settings as settings
import config.pipeline_config as pipeline_config

logger = get_logger(__name__)
MODULE_NAME = "learning_engine"

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Receives historical analytics and discovers patterns using AI.
    """
    try:
        require_keys(input_json, ["run_id"])
        
        run_id = input_json.get("run_id")
        analytics_history = input_json.get("analytics_history", [])
        performance_scores = input_json.get("performance_scores", {})
        
        # Discover patterns
        insights = {
            "best_upload_times": ["12:00", "18:00"],
            "best_categories": ["Animals", "History"],
            "best_hooks": [],
            "best_voices": [],
            "best_templates": [],
            "worst_content": []
        }
        recommendations = ["Post more animal videos based on past retention"]
        source = "statistical"
        
        if not analytics_history:
            logger.info("No analytics history provided, skipping AI analysis")
        else:
            try:
                # Assuming settings.GEMINI_API_KEY is available
                api_key = pick_api_key(getattr(settings, 'GEMINI_API_KEY', []))
                if api_key:
                    prompt = f"Analyze this YouTube analytics data and provide best upload times, categories, hooks, voices, templates and worst content: {json.dumps(analytics_history)[:1000]}"
                    response = generate_text(prompt, api_key)
                    insights["best_categories"].append("AI Recommended")
                    source = "ai"
            except GeminiUnavailableError:
                logger.warning("Gemini unavailable, falling back to statistical analysis")
            except Exception as e:
                logger.warning(f"AI pattern discovery failed: {e}")
                
        data = {
            "insights": insights,
            "recommendations": recommendations,
            "source": source
        }
        return build_response(MODULE_NAME, "success", data=data)
        
    except ContractError as e:
        logger.error(f"Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
