import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional
from sqlalchemy.orm import Session

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "security_audit.log")

# Ensure the log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Configure the audit logger with dual emission: stdout + rotating file
logger = logging.getLogger("security_audit")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter("%(message)s")

# 1. Rotating File Handler (1MB file size limit, 5 backup files)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=5)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 2. Standard Output Stream Handler for centralized log collection (FluentBit, Datadog)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


def log_audit_event(
    action: str,
    username: str,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
    db: Optional[Session] = None,
):
    """
    Logs security audit events with dual emission:
    1. Outputs structured JSON to sys.stdout and rotating log file.
    2. Persists the authoritative audit record in PostgreSQL 'audit_logs' table.
    """
    now = datetime.now(timezone.utc)
    log_entry = {
        "timestamp": now.isoformat(),
        "action": action,
        "username": username,
        "ip_address": ip_address,
        "details": details,
    }
    logger.info(json.dumps(log_entry))

    # Persist authoritative audit record to PostgreSQL
    try:
        from app.models.audit_log import AuditLog
        from app.repositories.audit_log_repository import AuditLogRepository
        from app.db.session import SessionLocal

        audit_record = AuditLog(
            action=action,
            username=username,
            ip_address=ip_address,
            details=details,
        )

        if db is not None:
            repo = AuditLogRepository(db)
            repo.create(audit_record)
        else:
            with SessionLocal() as db_session:
                repo = AuditLogRepository(db_session)
                repo.create(audit_record)
    except Exception as db_err:
        # Non-fatal notice: Do not fail primary business transaction if audit log DB insertion fails
        print(f"Notice: Could not persist AuditLog to PostgreSQL ({db_err}).")


def get_all_audit_logs(
    db: Optional[Session] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """
    Retrieves security audit logs with pagination support.
    Reads authoritatively from PostgreSQL when db is provided,
    falling back to local file logs or SessionLocal if db is omitted.
    Returns logs sorted chronologically descending.
    """
    if db is not None:
        try:
            from app.repositories.audit_log_repository import AuditLogRepository

            repo = AuditLogRepository(db)
            logs = repo.list(limit=limit, offset=offset)
            if logs:
                return [
                    {
                        "timestamp": (
                            log.created_at.isoformat()
                            if log.created_at
                            else datetime.now(timezone.utc).isoformat()
                        ),
                        "action": log.action,
                        "username": log.username,
                        "ip_address": log.ip_address,
                        "details": log.details,
                    }
                    for log in logs
                ]
        except Exception as e:
            print(f"Notice: Failed to fetch audit logs from DB, falling back to file: {e}")

    # Fallback to local rotating log file (used when db is omitted or in offline/testing setups)
    if os.path.exists(LOG_FILE):
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
            if parsed_logs:
                reversed_logs = list(reversed(parsed_logs))
                return reversed_logs[offset : offset + limit]
        except Exception:
            pass

    # Final fallback if file is empty or missing and db was None: SessionLocal
    try:
        from app.repositories.audit_log_repository import AuditLogRepository
        from app.db.session import SessionLocal

        with SessionLocal() as db_session:
            repo = AuditLogRepository(db_session)
            logs = repo.list(limit=limit, offset=offset)
            return [
                {
                    "timestamp": (
                        log.created_at.isoformat()
                        if log.created_at
                        else datetime.now(timezone.utc).isoformat()
                    ),
                    "action": log.action,
                    "username": log.username,
                    "ip_address": log.ip_address,
                    "details": log.details,
                }
                for log in logs
            ]
    except Exception:
        return []

