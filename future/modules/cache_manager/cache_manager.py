import os
import json
import time
import shutil
from typing import Dict, Any
from shared.logger import get_logger
from shared.json_contract import build_response, require_keys, ContractError
from shared.retry import retry
from shared.path_utils import safe_path, sanitize_filename, PROJECT_ROOT

MODULE_NAME = "cache_manager"
logger = get_logger(MODULE_NAME)

def _get_cache_dir():
    cache_dir = safe_path(PROJECT_ROOT, "cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir

@retry(max_attempts=3, base_delay_seconds=1.0, backoff_multiplier=2.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id"])
        action = input_json.get("action", "get")
        cache_dir = _get_cache_dir()
        
        if action == "set":
            require_keys(input_json, ["cache_type", "cache_key", "cache_value"])
            safe_type = sanitize_filename(input_json["cache_type"])
            safe_key = sanitize_filename(input_json["cache_key"])
            ttl_seconds = input_json.get("ttl_seconds", 86400)
            
            type_dir = safe_path(cache_dir, safe_type)
            type_dir.mkdir(parents=True, exist_ok=True)
            
            file_path = safe_path(type_dir, f"{safe_key}.json")
            data = {
                "value": input_json["cache_value"],
                "created_at": time.time(),
                "expires_at": time.time() + ttl_seconds,
                "access_count": 0
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
                
            return build_response(MODULE_NAME, "success", data={"stored": True})
            
        elif action == "get":
            require_keys(input_json, ["cache_type", "cache_key"])
            safe_type = sanitize_filename(input_json["cache_type"])
            safe_key = sanitize_filename(input_json["cache_key"])
            
            file_path = safe_path(cache_dir, safe_type, f"{safe_key}.json")
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                if time.time() > data.get("expires_at", 0):
                    file_path.unlink()
                    return build_response(MODULE_NAME, "success", data={"value": None, "hit": False})
                    
                data["access_count"] = data.get("access_count", 0) + 1
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                    
                return build_response(MODULE_NAME, "success", data={"value": data["value"], "hit": True})
            return build_response(MODULE_NAME, "success", data={"value": None, "hit": False})
            
        elif action == "delete":
            require_keys(input_json, ["cache_type", "cache_key"])
            safe_type = sanitize_filename(input_json["cache_type"])
            safe_key = sanitize_filename(input_json["cache_key"])
            file_path = safe_path(cache_dir, safe_type, f"{safe_key}.json")
            if file_path.exists():
                file_path.unlink()
            return build_response(MODULE_NAME, "success", data={"deleted": True})
            
        elif action == "clear":
            cache_type = input_json.get("cache_type")
            if cache_type:
                safe_type = sanitize_filename(cache_type)
                type_dir = safe_path(cache_dir, safe_type)
                if type_dir.exists():
                    shutil.rmtree(type_dir)
            else:
                for item in cache_dir.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
            return build_response(MODULE_NAME, "success", data={"cleared": True})
            
        elif action == "stats":
            total_entries = 0
            total_size_bytes = 0
            expired_count = 0
            
            for root, _, files in os.walk(cache_dir):
                for file in files:
                    if file.endswith('.json'):
                        file_path = os.path.join(root, file) # os.walk still used for recursive search, but root is safe
                        try:
                            total_size_bytes += os.path.getsize(file_path)
                            with open(file_path, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                                total_entries += 1
                                if time.time() > data.get('expires_at', 0):
                                    expired_count += 1
                        except:
                            pass
                            
            return build_response(MODULE_NAME, "success", data={
                "total_entries": total_entries,
                "total_size_bytes": total_size_bytes,
                "expired_count": expired_count
            })
            
        else:
            raise Exception(f"Unknown action: {action}")

    except ContractError as ce:
        logger.error(f"Contract error: {ce}")
        return build_response(MODULE_NAME, "error", error=str(ce))
    except Exception as e:
        logger.error(f"Error in cache_manager: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
