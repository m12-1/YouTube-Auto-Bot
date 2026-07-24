import uuid
import datetime
from typing import Dict, Any, List

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry

MODULE_NAME = 'scheduler'
logger = get_logger(__name__)

@retry(max_attempts=3, base_delay_seconds=1.0)
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enhanced Scheduler module.
    Generates run_id and started_at. Tracks scheduled jobs.
    """
    try:
        # Validate input
        # No specific required keys are strictly needed since we are generating them,
        # but triggered_by is useful.
        
        triggered_by = input_json.get('triggered_by', 'system')
        category_hint = input_json.get('category_hint', None)
        timezone = input_json.get('timezone', 'UTC')
        
        daily_schedule = input_json.get('daily_schedule')
        weekly_schedule = input_json.get('weekly_schedule')
        monthly_jobs = input_json.get('monthly_jobs')
        analytics_collection_interval = input_json.get('analytics_collection_interval')
        cleanup_interval = input_json.get('cleanup_interval')
        
        run_id = str(uuid.uuid4())
        started_at = datetime.datetime.utcnow().isoformat() + "Z"
        
        # Build scheduled_jobs list for info
        scheduled_jobs: List[Dict[str, Any]] = []
        if daily_schedule:
            scheduled_jobs.append({"type": "daily", "pattern": daily_schedule})
        if weekly_schedule:
            scheduled_jobs.append({"type": "weekly", "pattern": weekly_schedule})
        if monthly_jobs:
            scheduled_jobs.append({"type": "monthly", "pattern": monthly_jobs})
        if analytics_collection_interval:
            scheduled_jobs.append({"type": "analytics", "pattern": analytics_collection_interval})
        if cleanup_interval:
            scheduled_jobs.append({"type": "cleanup", "pattern": cleanup_interval})
            
        data = {
            'run_id': run_id,
            'triggered_by': triggered_by,
            'started_at': started_at,
            'category_hint': category_hint,
            'scheduled_jobs': scheduled_jobs,
            'timezone': timezone
        }
        
        logger.info(f"Generated run_id: {run_id} triggered by {triggered_by}")
        
        return build_response(module=MODULE_NAME, status='success', data=data)
        
    except ContractError as e:
        logger.error(f"Contract error in scheduler: {e}")
        return build_response(module=MODULE_NAME, status='error', error=str(e))
    except Exception as e:
        logger.error(f"Unexpected error in scheduler: {e}", exc_info=True)
        return build_response(module=MODULE_NAME, status='error', error=str(e))
