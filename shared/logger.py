"""
shared/logger.py

Centralized logger factory for the entire platform.
Every module must obtain its logger through `get_logger(name)` so that
log formatting, level, and output destinations stay consistent
across the whole codebase.

No module should call `logging.basicConfig` directly — configuration
is centralized here and driven by `config.settings.LOG_LEVEL`.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

_CONFIGURED = False


def _configure_root_logger(log_level: str) -> None:
    """
    Configure the root logger exactly once for the whole process.

    Args:
        log_level: Logging level name, e.g. "INFO", "DEBUG", "WARNING".
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    
    # We create a custom formatter to output JSON for structured logging
    class StructuredFormatter(logging.Formatter):
        def format(self, record):
            import json
            log_obj = {
                "timestamp": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()
            }
            if hasattr(record, "run_id"):
                log_obj["run_id"] = record.run_id
            if hasattr(record, "module_name"):
                log_obj["module_name"] = record.module_name
            if hasattr(record, "execution_time"):
                log_obj["execution_time"] = record.execution_time
            if hasattr(record, "warning_count"):
                log_obj["warning_count"] = record.warning_count
                
            if record.exc_info:
                log_obj["exception"] = self.formatException(record.exc_info)
                
            return json.dumps(log_obj)

    formatter = StructuredFormatter(datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers if this module is re-imported.
    if not root_logger.handlers:
        root_logger.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str, log_level: Optional[str] = None) -> logging.Logger:
    """
    Return a configured logger instance for a given module name.

    Args:
        name: Usually `__name__` of the calling module.
        log_level: Optional override for the log level. Falls back to
            `config.settings.LOG_LEVEL` when not provided.

    Returns:
        A ready-to-use `logging.Logger` instance.
    """
    if log_level is None:
        try:
            # Local import to avoid circular imports between
            # shared.logger and config.settings.
            from config.settings import LOG_LEVEL

            log_level = LOG_LEVEL
        except Exception:  # pragma: no cover - safe fallback
            log_level = "INFO"

    _configure_root_logger(log_level)
    return logging.getLogger(name)
