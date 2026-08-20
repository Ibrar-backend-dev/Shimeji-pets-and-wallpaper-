"""Attaches the resolved API client to every request, before any view runs."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .authentication import resolve_from_request, touch_client


class AppKeyMiddleware:
    """
    Set `request.app_client` to a ResolvedClient, or None.

    Runs for every request so both the permission classes and the access log can
    rely on the attribute existing. Invalid keys are not rejected here — that is
    a permission decision, and rejecting in middleware would also break the
    admin, which uses no key at all.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        resolved = resolve_from_request(request)
        request.app_client = resolved
        if resolved is not None:
            touch_client(resolved)
        return self.get_response(request)
