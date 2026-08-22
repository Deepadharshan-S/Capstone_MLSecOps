import os
import sys
import time
import threading
from collections import defaultdict
from fastapi import Request, HTTPException, status
from app.core.logging_config import log_audit_event


class RateLimiter:
    """
    An in-memory, thread-safe sliding window rate limiter implemented as a FastAPI dependency.
    Bypasses rate limiting in testing environments by default unless force_enable=True.
    """

    def __init__(self, times: int, seconds: int = 60, force_enable: bool = False):
        self.times = times
        self.seconds = seconds
        self.force_enable = force_enable
        # key: (ip_address, endpoint_path) -> value: list of timestamps
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def _is_testing(self) -> bool:
        return "pytest" in sys.modules or os.getenv("TESTING") in ("true", "True", "1")

    def __call__(self, request: Request):
        if self._is_testing() and not self.force_enable:
            return

        ip_address = request.client.host if request.client else "127.0.0.1"
        path = request.url.path
        key = (ip_address, path)
        now = time.time()

        with self.lock:
            # Keep only the timestamps within the sliding window
            self.requests[key] = [
                t for t in self.requests[key] if now - t < self.seconds
            ]

            if len(self.requests[key]) >= self.times:
                # Log audit event for security monitoring
                log_audit_event(
                    action="rate_limit_exceeded",
                    username="anonymous",
                    ip_address=ip_address,
                    details=f"Rate limit of {self.times} requests per {self.seconds} seconds exceeded on {path}.",
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again later.",
                )

            self.requests[key].append(now)
