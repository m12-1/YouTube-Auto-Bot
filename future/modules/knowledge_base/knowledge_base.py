import os
import json
import uuid
from typing import Dict, Any, List

from shared.json_contract import build_response, require_keys, ContractError
from shared.logger import get_logger
from shared.retry import retry

logger = get_logger(__name__)
MODULE_NAME = "knowledge_base"

def _word_overlap_score(text1: str, text2: str) -> float:
    words1 = set(str(text1).lower().split())
    words2 = set(str(text2).lower().split())
    if not words1 or not words2:
        return 0.0
    return len(words1.intersection(words2)) / float(max(len(words1), len(words2)))

@retry(max_attempts=3, base_delay_seconds=1.0, exceptions=(Exception,))
def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stores and retrieves historical data with semantic similarity.
    """
    try:
        require_keys(input_json, ["run_id"])
        
        action = input_json.get("action", "query")
        entry_type = input_json.get("entry_type", "topic")
        entry_data = input_json.get("entry_data", {})
        query_text = input_json.get("query_text", "")
        
        db_path = f"db/knowledge/{entry_type}"
        os.makedirs(db_path, exist_ok=True)
        
        existing_entries = []
        for f in os.listdir(db_path):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(db_path, f), 'r') as fh:
                        existing_entries.append((f, json.load(fh)))
                except json.JSONDecodeError:
                    continue
                    
        data = {}
        if action == "store":
            is_dup = False
            text_to_check = str(entry_data)
            for f, entry in existing_entries:
                if _word_overlap_score(text_to_check, str(entry)) > 0.9:
                    is_dup = True
                    break
            
            if not is_dup:
                file_id = str(uuid.uuid4())
                with open(os.path.join(db_path, f"{file_id}.json"), 'w') as fh:
                    json.dump(entry_data, fh)
                data = {"stored": True, "duplicate_found": False}
            else:
                data = {"stored": False, "duplicate_found": True}
                
        elif action == "check_duplicate":
            similar_entries = []
            text_to_check = str(entry_data)
            for f, entry in existing_entries:
                score = _word_overlap_score(text_to_check, str(entry))
                if score > 0.8:
                    similar_entries.append({"entry": entry, "score": score})
            
            data = {
                "is_duplicate": len(similar_entries) > 0,
                "similar_entries": similar_entries
            }
            
        else: # query
            results = []
            for f, entry in existing_entries:
                score = _word_overlap_score(query_text, str(entry))
                if score > 0.3:
                    results.append({"entry": entry, "score": score})
            results.sort(key=lambda x: x["score"], reverse=True)
            data = {"results": results}
            
        return build_response(MODULE_NAME, "success", data=data)
        
    except ContractError as e:
        logger.error(f"Contract error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return build_response(MODULE_NAME, "error", error=str(e))
