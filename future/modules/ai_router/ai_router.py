import time
from typing import Dict, Any
from shared.logger import get_logger
from shared.json_contract import build_response, require_keys, ContractError
from shared.retry import retry
from shared import gemini_client
import config.settings as settings

MODULE_NAME = "ai_router"
logger = get_logger(MODULE_NAME)

_KEY_QUOTA = {}
_KEY_COOLDOWN = {}
COOLDOWN_SECONDS = 300

def _get_best_model(task_type: str) -> str:
    if task_type == 'script_generation':
        return 'gemini-1.5-pro'
    elif task_type == 'review':
        return 'gemini-1.5-pro'
    elif task_type == 'verification':
        return 'gemini-1.5-pro'
    else:
        return 'gemini-1.5-flash'

def _get_available_keys() -> list:
    keys = [
        getattr(settings, 'GEMINI_KEY_ADVANCED', None),
        getattr(settings, 'GEMINI_KEY_FILTER', None),
        getattr(settings, 'GEMINI_KEY_FILTER_2', None),
        getattr(settings, 'GEMINI_KEY_IMAGE', None),
        getattr(settings, 'GEMINI_KEY_LIGHT', None)
    ]
    return [k for k in keys if k]

@retry(max_attempts=3, base_delay_seconds=1.0, backoff_multiplier=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id", "prompt"])
        
        run_id = input_json["run_id"]
        prompt = input_json["prompt"]
        task_type = input_json.get("task_type", "general")
        
        # Cleanup cooldowns
        now = time.time()
        for k in list(_KEY_COOLDOWN.keys()):
            if now - _KEY_COOLDOWN[k] > COOLDOWN_SECONDS:
                del _KEY_COOLDOWN[k]
                
        available_keys = _get_available_keys()
        valid_keys = [k for k in available_keys if k not in _KEY_COOLDOWN]
        
        if not valid_keys:
            raise Exception("No available API keys (all in cooldown or missing)")
            
        selected_key = valid_keys[0] # Simplistic rotation
        selected_model = _get_best_model(task_type)
        
        try:
            response_text = gemini_client.generate_text(
                prompt=prompt,
                api_key=selected_key,
                model=selected_model
            )
            _KEY_QUOTA[selected_key] = _KEY_QUOTA.get(selected_key, 0) + 1
            
            return build_response(MODULE_NAME, "success", data={
                "response_text": response_text,
                "provider_used": "gemini",
                "model_used": selected_model,
                "api_key_pool": len(valid_keys),
                "attempt_count": 1,
                "source": "api"
            })
        except Exception as e:
            logger.error(f"Failed with key, putting in cooldown: {str(e)}")
            _KEY_COOLDOWN[selected_key] = time.time()
            raise e

    except ContractError as ce:
        logger.error(f"Contract error: {ce}")
        return build_response(MODULE_NAME, "error", error=str(ce))
    except Exception as e:
        logger.error(f"Error in ai_router: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
