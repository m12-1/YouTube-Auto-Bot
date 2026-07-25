import os
import shutil
from typing import Dict, Any

try:
    import psutil
except ImportError:
    psutil = None

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry

MODULE_NAME = "monitoring"
logger = get_logger(__name__)

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id"])
        
        action = input_json.get("action", "check")
        check_type = input_json.get("check_type", "all")
        
        alerts = []
        metrics = {
            "disk_usage_percent": 0.0,
            "memory_usage_mb": 0.0,
            "module_statuses": {},
            "api_status": "ok"
        }
        
        base_dir = "C:/Users/aya0s/Downloads/youtube_shorts_extract/youtube_shorts_platform"
        if os.path.exists(base_dir):
            total, used, free = shutil.disk_usage(base_dir)
            metrics["disk_usage_percent"] = (used / total) * 100
            if metrics["disk_usage_percent"] > 90:
                alerts.append("High disk usage detected.")
        
        if psutil:
            mem = psutil.virtual_memory()
            metrics["memory_usage_mb"] = mem.used / (1024 * 1024)
            if mem.percent > 90:
                alerts.append("High memory usage detected.")
        
        health_status = {
            "status": "healthy" if not alerts else "warning"
        }
        
        return build_response(
            module=MODULE_NAME,
            status="success",
            data={
                "health_status": health_status,
                "alerts": alerts,
                "metrics": metrics
            }
        )
    except ContractError as e:
        logger.error(f"Contract error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
