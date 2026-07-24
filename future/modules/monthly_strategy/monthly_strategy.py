import os
import json
from typing import Dict, Any

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)
MODULE_NAME = "monthly_strategy"

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generates monthly publishing strategy.
    """
    try:
        require_keys(input_json, ["run_id"])
        
        run_id = input_json.get("run_id")
        insights = input_json.get("insights", {})
        month = input_json.get("month", 1)
        year = input_json.get("year", 2024)
        total_uploads_target = input_json.get("total_uploads_target", 36)
        
        # Calculate strategy
        best_categories = insights.get("best_categories", ["Animals", "History", "Psychology", "Money"])
        num_categories = len(best_categories)
        alloc_per_category = total_uploads_target // max(1, num_categories)
        
        schedule = {cat: alloc_per_category for cat in best_categories}
        if schedule and best_categories:
            schedule[best_categories[0]] += total_uploads_target % max(1, num_categories)
            
        publishing_calendar = [
            {"day": i+1, "category": best_categories[i % max(1, num_categories)]}
            for i in range(total_uploads_target)
        ]
        
        strategy = {
            "monthly_plan": schedule,
            "publishing_calendar": publishing_calendar,
            "schedule": schedule,
            "diversity_score": 0.85
        }
        recommendations = ["Stick to the schedule, prioritizing historically successful categories."]
        
        data = {
            "strategy": strategy,
            "recommendations": recommendations
        }
        return build_response(MODULE_NAME, "success", data=data)
        
    except ContractError as e:
        logger.error(f"Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
