import os
import time
from typing import Dict, Any

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
from shared.path_utils import safe_path, PROJECT_ROOT

MODULE_NAME = "cleanup_manager"
logger = get_logger(__name__)

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id"])
        
        action = input_json.get("action", "clean_all")
        max_age_hours = input_json.get("max_age_hours", 24)
        dry_run = input_json.get("dry_run", False)
        
        base_dir = PROJECT_ROOT
        
        dirs_to_clean = {
            "temp": safe_path(base_dir, "temp"),
            "cache": safe_path(base_dir, "cache", "media"),
            "failed": safe_path(base_dir, "output")
        }
        
        files_deleted = []
        total_bytes_freed = 0
        categories_cleaned = []
        
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        
        for category, dir_path in dirs_to_clean.items():
            if action in [f"clean_{category}", "clean_all"] and dir_path.exists():
                categories_cleaned.append(category)
                for file_path in dir_path.rglob("*"):
                    if not file_path.is_file():
                        continue
                    if category == "failed" and "failed" not in file_path.name.lower():
                        continue
                            
                        try:
                            file_age = current_time - file_path.stat().st_mtime
                            if file_age > max_age_seconds:
                                size = file_path.stat().st_size
                                files_deleted.append(str(file_path))
                                total_bytes_freed += size
                                if not dry_run:
                                    file_path.unlink()
                        except Exception as e:
                            logger.warning(f"Failed to process {file_path}: {e}")
        
        return build_response(
            module=MODULE_NAME,
            status="success",
            data={
                "cleaned": {
                    "files_deleted": files_deleted,
                    "total_bytes_freed": total_bytes_freed,
                    "categories_cleaned": categories_cleaned
                },
                "dry_run": dry_run
            }
        )
    except ContractError as e:
        logger.error(f"Contract error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
