import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "security_audit.log")

# Ensure the log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Configure the rotating logger
logger = logging.getLogger("security_audit")
logger.setLevel(logging.INFO)
logger.handlers.clear()

# 1MB file size limit, 5 backup files
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=5)
formatter = logging.Formatter("%(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def log_audit_event(
    action: str,
    username: str,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
):
    """
    Logs security audit events to a rotated log file in structured JSON format.
    """
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "username": username,
        "ip_address": ip_address,
        "details": details,
    }
    logger.info(json.dumps(log_entry))


def get_all_audit_logs() -> list[dict]:
    """
    Reads the security audit log file and parses its JSON lines.
    Returns logs sorted chronologically descending.
    """
    if not os.path.exists(LOG_FILE):
        return []

    parsed_logs = []
    try:
        with open(LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        parsed_logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass

    return list(reversed(parsed_logs))
