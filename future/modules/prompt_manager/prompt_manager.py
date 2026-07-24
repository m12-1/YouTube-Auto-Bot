import os
from typing import Dict, Any
from shared.logger import get_logger
from shared.json_contract import build_response, require_keys, ContractError
from shared.retry import retry

MODULE_NAME = "prompt_manager"
logger = get_logger(MODULE_NAME)

_PROMPT_CACHE = {}

@retry(max_attempts=3, base_delay_seconds=1.0, backoff_multiplier=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id", "prompt_name"])
        
        prompt_name = input_json["prompt_name"]
        template_vars = input_json.get("template_vars", {})
        version = input_json.get("version", "latest")
        bypass_cache = input_json.get("bypass_cache", False)
        
        cache_key = f"{prompt_name}_{version}"
        cached = False
        
        if not bypass_cache and cache_key in _PROMPT_CACHE:
            prompt_template = _PROMPT_CACHE[cache_key]
            cached = True
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            if version == "latest":
                prompt_path = os.path.join(base_dir, "prompts", f"{prompt_name}.txt")
            else:
                prompt_path = os.path.join(base_dir, "prompts", version, f"{prompt_name}.txt")
                
            if not os.path.exists(prompt_path):
                raise Exception(f"Prompt template not found at {prompt_path}")
                
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt_template = f.read()
                
            _PROMPT_CACHE[cache_key] = prompt_template
            
        rendered_prompt = prompt_template
        for k, v in template_vars.items():
            rendered_prompt = rendered_prompt.replace(f"{{{k}}}", str(v))
            
        return build_response(MODULE_NAME, "success", data={
            "rendered_prompt": rendered_prompt,
            "prompt_name": prompt_name,
            "version": version,
            "cached": cached,
            "template_vars_used": list(template_vars.keys())
        })

    except ContractError as ce:
        logger.error(f"Contract error: {ce}")
        return build_response(MODULE_NAME, "error", error=str(ce))
    except Exception as e:
        logger.error(f"Error in prompt_manager: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
