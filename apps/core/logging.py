"""
Structured logging.

Logs go to stdout as one JSON object per line, which is what Render and Railway
capture and what makes a field greppable instead of a substring of prose. The
current request id lives in a contextvar so any log call anywhere in the request
carries it without being handed the request object.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from contextvars import ContextVar

# Set by RequestIDMiddleware; read by the filter below.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# LogRecord attributes that are structural, not payload. Everything else a
# caller passes via `extra=` is merged into the JSON object.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName request_id""".split()
)


class RequestIDFilter(logging.Filter):
    """Attach the ambient request id to every record, so formatters can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": dt.datetime.fromtimestamp(
                record.created, tz=dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # default=str keeps UUIDs, datetimes and Decimals from blowing up a log call.
        return json.dumps(payload, default=str, ensure_ascii=False)
