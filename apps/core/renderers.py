"""
The response envelope.

Every response body in this API looks like:

    {"status": 200, "data": {...}, "message": "Wallpapers fetched successfully"}

Wrapping happens here, in one renderer, rather than in each view. Views return
plain data and the envelope is applied on the way out, so it is impossible for a
view to forget it or to spell it differently.
"""

from __future__ import annotations

from typing import Any

from rest_framework.renderers import JSONRenderer

# Marker key. A view (or the exception handler) that has already built a full
# envelope sets this so the renderer passes the body through untouched.
ENVELOPE_MARKER = "__envelope__"

_DEFAULT_MESSAGES = {
    "GET": "Fetched successfully",
    "POST": "Created successfully",
    "PUT": "Updated successfully",
    "PATCH": "Updated successfully",
    "DELETE": "Deleted successfully",
}


def build_envelope(
    status: int,
    data: Any = None,
    message: str = "",
    errors: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Assemble an envelope dict. Used by the exception handler too."""
    envelope: dict[str, Any] = {
        "status": status,
        "data": data,
        "message": message,
    }
    if errors is not None:
        envelope["errors"] = errors
    if request_id:
        envelope["request_id"] = request_id
    return envelope


class EnvelopeJSONRenderer(JSONRenderer):
    """Wrap whatever the view returned in the standard envelope."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}
        response = renderer_context.get("response")

        # No response object (e.g. rendering in isolation) — nothing to wrap against.
        if response is None:
            return super().render(data, accepted_media_type, renderer_context)

        # Already enveloped upstream: strip the marker and emit as-is.
        if isinstance(data, dict) and data.get(ENVELOPE_MARKER):
            body = {k: v for k, v in data.items() if k != ENVELOPE_MARKER}
            return super().render(body, accepted_media_type, renderer_context)

        view = renderer_context.get("view")
        request = renderer_context.get("request")

        envelope = build_envelope(
            status=response.status_code,
            data=data,
            message=self._message_for(view, request, response),
        )
        return super().render(envelope, accepted_media_type, renderer_context)

    @staticmethod
    def _message_for(view: Any, request: Any, response: Any) -> str:
        """
        Resolve the human-readable message.

        Precedence: an explicit message set on the response, then a per-view
        `success_message`, then a generic verb-based default. Views therefore
        only declare a message when they have something specific to say.
        """
        explicit = getattr(response, "success_message", None)
        if explicit:
            return str(explicit)

        from_view = getattr(view, "success_message", None)
        if from_view:
            return str(from_view)

        method = getattr(request, "method", "GET") or "GET"
        return _DEFAULT_MESSAGES.get(method.upper(), "OK")
