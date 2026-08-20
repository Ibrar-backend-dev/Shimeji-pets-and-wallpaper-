"""Request-scoped middleware: correlation ids and the access log."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .logging import request_id_var

access_logger = logging.getLogger("core.access")

# Health probes fire constantly and say nothing. Logging them buries real traffic.
_UNLOGGED_PATHS = frozenset({"/healthz", "/readyz"})

_MAX_INBOUND_ID_LEN = 64


class RequestIDMiddleware:
    """
    Bind a correlation id to the request.

    Honours an inbound X-Request-ID so a trace can span the CDN and the app, but
    truncates it: the value ends up in logs and in error envelopes, and an
    unbounded client-supplied string in a log line is a log-injection vector.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        inbound = request.headers.get("X-Request-ID", "")
        # Keep only characters that are safe in a log field.
        cleaned = "".join(ch for ch in inbound if ch.isalnum() or ch in "-_")[
            :_MAX_INBOUND_ID_LEN
        ]
        request_id = cleaned or uuid.uuid4().hex

        request.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)

        response["X-Request-ID"] = request_id
        return response


class AccessLogMiddleware:
    """
    One structured line per request.

    Sits last in MIDDLEWARE so it observes the final status code after every
    other middleware has had its say.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path in _UNLOGGED_PATHS:
            return self.get_response(request)

        started = time.perf_counter()
        response = self.get_response(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        # Populated by AppKeyAuthentication when a valid X-API-Key was presented.
        app_client = getattr(request, "app_client", None)

        if response.status_code >= 500:
            level = logging.ERROR
        elif response.status_code >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO

        access_logger.log(
            level,
            "%s %s %s",
            request.method,
            request.path,
            response.status_code,
            extra={
                "event": "http_access",
                "method": request.method,
                "path": request.path,
                "query": request.META.get("QUERY_STRING", "")[:512],
                "status": response.status_code,
                "duration_ms": duration_ms,
                "app_client": getattr(app_client, "slug", None),
                # Streaming responses have no .content; reading it would consume them.
                "response_bytes": (
                    len(response.content) if hasattr(response, "content") else None
                ),
            },
        )
        return response
