"""
shared/json_contract.py

Base Pydantic models, validation utilities, and backward-compatible 
JSON contract helpers for the platform.
"""

from __future__ import annotations

import time
import functools
from typing import Any, Callable, Dict, Iterable, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

# For backward compatibility during gradual Pydantic migration
class ContractError(ValueError):
    """Raised when an input JSON object does not satisfy a module's contract."""


def require_keys(payload: Dict[str, Any], required: Iterable[str]) -> None:
    """
    Ensure that all required keys are present (and not None) in a payload.
    Used by modules not yet migrated to Pydantic.
    """
    if not isinstance(payload, dict):
        raise ContractError(f"Expected input_json to be a dict, got {type(payload)}")

    missing = [key for key in required if payload.get(key) is None]
    if missing:
        raise ContractError(f"Missing required keys in input_json: {missing}")


def build_response(
    module: str,
    status: str,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    execution_time: float = 0.0,
) -> Dict[str, Any]:
    """
    Build a standardized JSON response envelope.
    Supports both legacy format and the new Phase 1 Refactor format.
    """
    if status not in ("success", "error"):
        raise ValueError("status must be either 'success' or 'error'")

    # Merged format satisfying both legacy code and new requirements
    return {
        "status": status,          # Legacy
        "module": module,          # Legacy
        "success": status == "success",  # New
        "error": error,            # Shared (was sometimes None, now string/None)
        "message": error if status == "error" else "Success", # New
        "data": data or {},        # Shared
        "execution_time": execution_time, # New
        "stage": module,           # New
    }


# ==============================================================================
# PYDANTIC PHASE 1 REFACTOR SYSTEM
# ==============================================================================

class BaseModuleInput(BaseModel):
    """Base Pydantic model for module inputs."""
    run_id: str

class BaseModuleOutput(BaseModel):
    """Base Pydantic model for module outputs (new format)."""
    success: bool
    error: Optional[str] = None
    message: str
    data: Dict[str, Any]
    execution_time: float
    stage: str
    
    # Legacy compatibility fields
    status: str
    module: str

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)

def module_contract(
    input_model: Type[TIn],
    output_model: Type[TOut],
    module_name: str
) -> Callable[[Callable[[TIn], TOut]], Callable[[Dict[str, Any]], Dict[str, Any]]]:
    """
    Decorator for modules to automatically validate inputs and outputs using Pydantic,
    handle exceptions uniformly, measure execution time, and log results.
    """
    def decorator(func: Callable[[TIn], TOut]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        @functools.wraps(func)
        def wrapper(input_json: Dict[str, Any]) -> Dict[str, Any]:
            from shared.logger import get_logger
            logger = get_logger(module_name)
            
            run_id = input_json.get("run_id", "unknown")
            logger.info(f"START {module_name} for run_id={run_id}")
            start_time = time.time()
            
            try:
                # 1. Validate Input
                validated_input = input_model(**input_json)
                
                # 2. Execute
                result = func(validated_input)
                
                # 3. Time
                exec_time = time.time() - start_time
                
                # Ensure the result has standard fields updated (like execution_time)
                if hasattr(result, "execution_time"):
                    result.execution_time = exec_time
                if hasattr(result, "stage"):
                    result.stage = module_name
                    
                # 4. Log finish
                logger.info(
                    f"FINISH {module_name} for run_id={run_id}",
                    extra={"run_id": run_id, "module_name": module_name, "execution_time": exec_time, "warning_count": 0}
                )
                
                return result.model_dump()
                
            except ValidationError as ve:
                exec_time = time.time() - start_time
                logger.error(f"Validation error in {module_name}: {ve}")
                return build_response(
                    module=module_name,
                    status="error",
                    error=f"Validation failed: {ve}",
                    execution_time=exec_time
                )
            except Exception as e:
                exec_time = time.time() - start_time
                logger.exception(f"Unexpected error in {module_name}: {e}")
                return build_response(
                    module=module_name,
                    status="error",
                    error=str(e),
                    execution_time=exec_time
                )
        return wrapper
    return decorator
