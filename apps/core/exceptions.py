"""
Error handling.

Errors use the same envelope as successes, so a client parses one shape and
never has to branch on status code to find the payload:

    {"status": 400, "data": null, "message": "Invalid request",
     "errors": {"limit": ["Must be <= 100."]}, "request_id": "…"}

Anything unhandled becomes a generic 500 carrying only the request id — the
traceback goes to the logs, never to the client.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .logging import request_id_var
from .renderers import ENVELOPE_MARKER, build_envelope

logger = logging.getLogger("core.errors")


class ApiError(drf_exceptions.APIException):
    """
    An error with a message meant for a human reading the app.

    DRF's built-ins put their text under `detail`; this puts it in `message`
    where the envelope wants it, and keeps field errors separate in `errors`.
    """

    status_code = http_status.HTTP_400_BAD_REQUEST
    default_message = "Request could not be processed"

    def __init__(
        self,
        message: str | None = None,
        errors: Any = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.errors = errors
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail=self.message)


class InvalidQueryParams(ApiError):
    status_code = http_status.HTTP_400_BAD_REQUEST
    default_message = "Invalid query parameters"


class ResourceNotFound(ApiError):
    status_code = http_status.HTTP_404_NOT_FOUND
    default_message = "Resource not found"


class Conflict(ApiError):
    status_code = http_status.HTTP_409_CONFLICT
    default_message = "Conflicts with existing data"


_GENERIC_MESSAGES = {
    400: "Invalid request",
    401: "Authentication credentials were not provided or are invalid",
    403: "You do not have permission to perform this action",
    404: "Resource not found",
    405: "Method not allowed",
    406: "Requested representation is not available",
    409: "Conflicts with existing data",
    413: "Payload too large",
    415: "Unsupported media type",
    429: "Too many requests",
}


def _flatten_message(detail: Any) -> str:
    """Pull a single readable sentence out of DRF's nested detail structures."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        return _flatten_message(detail[0])
    if isinstance(detail, dict):
        for value in detail.values():
            found = _flatten_message(value)
            if found:
                return found
    return ""


def envelope_exception_handler(exc: Exception, context: dict) -> Response:
    """DRF EXCEPTION_HANDLER: render every failure in the standard envelope."""
    request_id = request_id_var.get()

    # Normalise the Django-native exceptions DRF does not handle by default.
    if isinstance(exc, DjangoValidationError):
        exc = drf_exceptions.ValidationError(detail=exc.message_dict
                                             if hasattr(exc, "message_dict")
                                             else list(exc.messages))
    elif isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = drf_exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)

    if response is None:
        # Genuinely unexpected. Log it with the traceback, tell the client nothing.
        view = context.get("view")
        request = context.get("request")
        logger.exception(
            "Unhandled exception in %s",
            getattr(view, "__class__", type(view)).__name__,
            extra={
                "event": "unhandled_exception",
                "path": getattr(request, "path", None),
                "method": getattr(request, "method", None),
                "exc_type": type(exc).__name__,
            },
        )
        body = build_envelope(
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            data=None,
            message="An unexpected error occurred. Quote the request_id when reporting it.",
            request_id=request_id,
        )
        body[ENVELOPE_MARKER] = True
        return Response(body, status=http_status.HTTP_500_INTERNAL_SERVER_ERROR)

    status_code = response.status_code
    detail = response.data

    if isinstance(exc, ApiError):
        message = exc.message
        errors = exc.errors
    elif isinstance(exc, drf_exceptions.ValidationError):
        # Field errors belong in `errors`; the message stays generic.
        message = "Invalid request"
        errors = detail
    else:
        message = _flatten_message(detail) or _GENERIC_MESSAGES.get(
            status_code, "Request failed"
        )
        errors = None

    # Throttling tells the client when to come back; surface it rather than bury it.
    if isinstance(exc, drf_exceptions.Throttled) and exc.wait is not None:
        errors = {"retry_after_seconds": int(exc.wait)}

    body = build_envelope(
        status=status_code,
        data=None,
        message=message,
        errors=errors,
        request_id=request_id,
    )
    body[ENVELOPE_MARKER] = True
    response.data = body
    return response
