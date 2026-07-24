import logging
import logging.handlers
import json
import os
from datetime import datetime

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger_name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineNo": record.lineno,
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)

def setup_logging(
    default_level=logging.INFO,
    log_dir="logs",
    log_file="app.log",
    max_bytes=10485760,  # 10MB
    backup_count=5,
    use_json=False,
    module_levels=None
):
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    root_logger = logging.getLogger()
    root_logger.setLevel(default_level)

    # Clear existing handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # File Handler
    file_path = os.path.join(log_dir, log_file)
    file_handler = logging.handlers.RotatingFileHandler(
        file_path, maxBytes=max_bytes, backupCount=backup_count
    )

    # Console Handler
    console_handler = logging.StreamHandler()

    if use_json:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Specific module levels
    if module_levels:
        for module, level in module_levels.items():
            logging.getLogger(module).setLevel(level)

    return root_logger
