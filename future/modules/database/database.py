import os
import json
from typing import Dict, Any

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry
from shared.path_utils import safe_path, sanitize_filename, PROJECT_ROOT

MODULE_NAME = "database"
logger = get_logger(__name__)

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        require_keys(input_json, ["run_id"])
        
        action = input_json.get("action", "get")
        collection = input_json.get("collection", "default")
        document_id = input_json.get("document_id")
        document_data = input_json.get("document_data", {})
        query_filter = input_json.get("query_filter", {})
        
        # Use project root and safe_path instead of hardcoded strings
        base_dir = PROJECT_ROOT
        db_backend = os.environ.get("DB_BACKEND", "json")
        
        safe_collection = sanitize_filename(collection)
        collection_dir = safe_path(base_dir, "db", safe_collection)
        collection_dir.mkdir(parents=True, exist_ok=True)
        
        if action == "set":
            if not document_id:
                raise ContractError("document_id required for set action")
            safe_doc_id = sanitize_filename(document_id)
            file_path = safe_path(collection_dir, f"{safe_doc_id}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(document_data, f, indent=2)
            return build_response(module=MODULE_NAME, status="success", data={"stored": True})
            
        elif action == "get":
            if not document_id:
                raise ContractError("document_id required for get action")
            safe_doc_id = sanitize_filename(document_id)
            file_path = safe_path(collection_dir, f"{safe_doc_id}.json")
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return build_response(module=MODULE_NAME, status="success", data={"document": data, "found": True})
            return build_response(module=MODULE_NAME, status="success", data={"document": None, "found": False})
            
        elif action == "delete":
            if not document_id:
                raise ContractError("document_id required for delete action")
            safe_doc_id = sanitize_filename(document_id)
            file_path = safe_path(collection_dir, f"{safe_doc_id}.json")
            if file_path.exists():
                file_path.unlink()
            return build_response(module=MODULE_NAME, status="success", data={"deleted": True})
            
        elif action == "list":
            documents = []
            for file in collection_dir.iterdir():
                if file.name.endswith(".json"):
                    with open(file, "r", encoding="utf-8") as f:
                        documents.append(json.load(f))
            return build_response(module=MODULE_NAME, status="success", data={"documents": documents, "count": len(documents)})
            
        elif action == "query":
            results = []
            for file in collection_dir.iterdir():
                if file.name.endswith(".json"):
                    with open(file, "r", encoding="utf-8") as f:
                        doc = json.load(f)
                        match = True
                        for k, v in query_filter.items():
                            if doc.get(k) != v:
                                match = False
                                break
                        if match:
                            results.append(doc)
            return build_response(module=MODULE_NAME, status="success", data={"results": results})
        else:
            raise ContractError(f"Unknown action: {action}")
            
    except ContractError as e:
        logger.error(f"Contract error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in {MODULE_NAME}: {e}")
        return build_response(module=MODULE_NAME, status="error", error=str(e))
