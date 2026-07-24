from typing import Dict, Any
from shared.logger import get_logger
from shared.json_contract import build_response, require_keys, ContractError
from shared.retry import retry
import config.settings as settings
import config.pipeline_config as pipeline_config

MODULE_NAME = "config_manager"
logger = get_logger(MODULE_NAME)

@retry(max_attempts=3, base_delay_seconds=1.0, backoff_multiplier=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id"])
        action = input_json.get("action", "get")
        
        if action == "get":
            require_keys(input_json, ["config_key"])
            config_key = input_json["config_key"]
            
            value = getattr(pipeline_config, config_key, None)
            if value is None:
                value = getattr(settings, config_key, None)
                
            return build_response(MODULE_NAME, "success", data={
                "value": value,
                "found": value is not None
            })
            
        elif action == "get_all":
            all_config = {}
            for k in dir(pipeline_config):
                if not k.startswith("_"):
                    all_config[k] = getattr(pipeline_config, k)
            for k in dir(settings):
                if not k.startswith("_"):
                    all_config[k] = getattr(settings, k)
            return build_response(MODULE_NAME, "success", data={"config": all_config})
            
        elif action == "validate":
            valid = True
            missing = []
            warnings = []
            
            # Basic validation
            if not getattr(settings, "GEMINI_KEY_ADVANCED", None):
                warnings.append("GEMINI_KEY_ADVANCED is missing")
                
            return build_response(MODULE_NAME, "success", data={
                "valid": valid,
                "missing": missing,
                "warnings": warnings
            })
            
        elif action == "check_secrets":
            missing_secrets = []
            if hasattr(settings, "check_missing_secrets"):
                missing_secrets = settings.check_missing_secrets()
            return build_response(MODULE_NAME, "success", data={
                "missing_secrets": missing_secrets
            })
            
        else:
            raise Exception(f"Unknown action: {action}")

    except ContractError as ce:
        logger.error(f"Contract error: {ce}")
        return build_response(MODULE_NAME, "error", error=str(ce))
    except Exception as e:
        logger.error(f"Error in config_manager: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
