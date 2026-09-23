import re
import uuid
from contextvars import ContextVar
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Context variable to hold the current request ID across async tasks and loggers
request_id_ctx_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# Pattern to validate incoming request IDs (alphanumeric, hyphens, underscores, max length 64)
_SAFE_REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def get_request_id() -> Optional[str]:
    """Retrieves the request ID for the current execution context."""
    return request_id_ctx_var.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces correlation IDs on incoming and outgoing requests:
    1. Reuses an incoming 'X-Request-ID' header if valid and safe.
    2. Otherwise generates a unique UUID4 hex correlation ID.
    3. Attaches the ID to request.state.request_id and contextvar.
    4. Sets the 'X-Request-ID' header on the HTTP response.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming_id = request.headers.get("X-Request-ID")
        if incoming_id and _SAFE_REQUEST_ID_REGEX.match(incoming_id.strip()):
            request_id = incoming_id.strip()
        else:
            request_id = uuid.uuid4().hex

        # Store in request.state and contextvar
        request.state.request_id = request_id
        token = request_id_ctx_var.set(request_id)

        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx_var.reset(token)
