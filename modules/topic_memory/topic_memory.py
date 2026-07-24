import os
from typing import Dict, Any, List

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
from modules.knowledge_base.knowledge_base import run as kb_run

logger = get_logger(__name__)
MODULE_NAME = "topic_memory"

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prevents topic repetition by checking against previous uploads.
    """
    try:
        require_keys(input_json, ["run_id", "topic"])
        
        run_id = input_json.get("run_id")
        topic = input_json.get("topic")
        hook = input_json.get("hook", "")
        title = input_json.get("title", "")
        script_text = input_json.get("script_text", "")
        threshold = input_json.get("similarity_threshold", 0.7)
        
        is_unique = True
        rejection_reasons = []
        similar_topics = []
        similar_hooks = []
        
        # Check topic
        kb_resp_topic = kb_run({
            "run_id": run_id,
            "action": "check_duplicate",
            "entry_type": "topic",
            "entry_data": {"topic": topic}
        })
        if kb_resp_topic.get("status") == "success":
            sims = kb_resp_topic["data"].get("similar_entries", [])
            for s in sims:
                if s["score"] > threshold:
                    is_unique = False
                    similar_topics.append(s["entry"])
                    rejection_reasons.append("Topic is too similar to past uploads.")
                    
        # Check hook
        if hook:
            kb_resp_hook = kb_run({
                "run_id": run_id,
                "action": "check_duplicate",
                "entry_type": "hook",
                "entry_data": {"hook": hook}
            })
            if kb_resp_hook.get("status") == "success":
                sims = kb_resp_hook["data"].get("similar_entries", [])
                for s in sims:
                    if s["score"] > threshold:
                        is_unique = False
                        similar_hooks.append(s["entry"])
                        rejection_reasons.append("Hook is too similar to past uploads.")
                        
        data = {
            "is_unique": is_unique,
            "similar_topics": similar_topics,
            "similar_hooks": similar_hooks,
            "rejection_reasons": rejection_reasons
        }
        
        return build_response(MODULE_NAME, "success", data=data)
        
    except ContractError as e:
        logger.error(f"Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
