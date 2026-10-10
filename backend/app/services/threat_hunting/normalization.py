import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional


SENSITIVE_FIELD_PATTERNS = [
    re.compile(r"pass(word)?", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"key", re.IGNORECASE),
    re.compile(r"auth(orization)?", re.IGNORECASE),
    re.compile(r"bearer", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"cookie", re.IGNORECASE),
]


def redact_sensitive_data(data: Any) -> Any:
    """
    Recursively redacts sensitive values (passwords, tokens, secrets, API keys)
    from structured event attributes.
    """
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            if any(pattern.search(str(k)) for pattern in SENSITIVE_FIELD_PATTERNS):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_data(v)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    elif isinstance(data, str):
        # Redact raw JWT tokens (e.g. eyJhbGciOi...)
        if len(data) > 64 and data.count(".") == 2 and data.startswith("eyJ"):
            return "[REDACTED_JWT]"
        # Redact Bearer prefix
        if data.lower().startswith("bearer "):
            return "[REDACTED_TOKEN]"
        return data
    return data


def compute_event_dedup_hash(
    source: str,
    event_type: str,
    actor_id: Optional[str],
    resource_id: Optional[str],
    occurrence_time: datetime,
    attributes: dict,
) -> str:
    """
    Computes a deterministic SHA-256 hash for exact duplicate detection.
    """
    utc_occ = occurrence_time.astimezone(timezone.utc).isoformat()
    # Normalize attributes key ordering
    attr_repr = json.dumps(attributes, sort_keys=True, default=str)
    raw = f"{source}|{event_type}|{actor_id or ''}|{resource_id or ''}|{utc_occ}|{attr_repr}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
