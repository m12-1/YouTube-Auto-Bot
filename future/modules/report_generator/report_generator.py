import os
import json
import csv
from datetime import datetime
from typing import Dict, Any

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
from shared.path_utils import safe_path, sanitize_filename, PROJECT_ROOT

MODULE_NAME = "report_generator"
logger = get_logger(__name__)

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id"])
        
        run_id = input_json["run_id"]
        report_type = input_json.get("report_type", "daily")
        export_format = input_json.get("export_format", "json")
        date_range_start = input_json.get("date_range_start")
        date_range_end = input_json.get("date_range_end")
        
        base_dir = PROJECT_ROOT
        analytics_dir = safe_path(base_dir, "db", "analytics")
        knowledge_dir = safe_path(base_dir, "db", "knowledge")
        reports_dir = safe_path(base_dir, "output", "reports")
        
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        report_data = {
            "uploads_count": 0,
            "failures_count": 0,
            "revenue_estimates": 0.0,
            "average_ctr": 0.0,
            "average_retention": 0.0,
            "best_videos": [],
            "worst_videos": [],
            "recommendations": ["Improve titles", "Add more hooks"]
        }
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename_base = f"report_{report_type}_{timestamp}"
        export_path = ""
        
        safe_filename = sanitize_filename(filename_base)
        
        if export_format == "json":
            export_path = safe_path(reports_dir, f"{safe_filename}.json")
            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(report_data, f, indent=2)
        elif export_format == "csv":
            export_path = safe_path(reports_dir, f"{safe_filename}.csv")
            with open(export_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(report_data.keys())
                writer.writerow([
                    report_data["uploads_count"],
                    report_data["failures_count"],
                    report_data["revenue_estimates"],
                    report_data["average_ctr"],
                    report_data["average_retention"],
                    ",".join(report_data["best_videos"]),
                    ",".join(report_data["worst_videos"]),
                    ",".join(report_data["recommendations"])
                ])
        else:
            export_path = safe_path(reports_dir, f"{safe_filename}.md")
            with open(export_path, 'w', encoding='utf-8') as f:
                f.write(f"# {report_type.capitalize()} Report\n\n")
                for k, v in report_data.items():
                    f.write(f"- **{k}**: {v}\n")
        
        return build_response(
            module=MODULE_NAME,
            status="success",
            data={
                "report": report_data,
                "export_path": str(export_path),
                "report_type": report_type,
                "export_format": export_format
            }
        )
    except ContractError as e:
        logger.error(f"Contract error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
