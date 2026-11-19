"""Logs: one JSON object per line, and keys, never content (manifesto rule 40).

Every line any process of the image writes (the server, uvicorn, the init container staging the keys, a
refused start, an uncaught exception, a Python warning) goes through `configure()` and is one JSON object:

    {"ts": "2026-11-19T10:00:00.000Z", "level": "INFO", "logger": "exchange_mcp.signin",
     "event": "sign_in_completed", "msg": "sign-in completed"}

`event` is a stable key: the one a call passes in `extra`, or else derived from the message template, so
it never carries the values formatted into the message. Further fields appear only when their name is on
`FIELDS`. A traceback is written as `exc` with its frames and exception types only: an exception's
message is someone's data as often as not, and is withheld.

As a second line of defence, a filter drops any record whose text holds something shaped like an
address, a JWT, an NTLM or bearer header, or a sealed blob, and counts the drop in
`mcp_log_records_dropped_total`, so a regression shows as a counter instead of a leak.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import traceback
from datetime import UTC, datetime

from exchange_mcp import metrics

_SHAPES = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+"  # an address
    r"|eyJ[\w-]{10,}\.[\w-]{10,}"  # a JWT
    r"|\b(?:NTLM|Negotiate|Bearer)\s+[A-Za-z0-9+/=._-]{16,}"  # an Authorization value
    r"|\b[\w-]{1,32}\.[A-Za-z0-9_-]{40,}"  # a sealed blob
)

# Fields a call may add with `extra=`; anything else stays out of the line.
FIELDS = frozenset({"tool", "op", "route_group", "status", "seconds", "count", "files", "mode", "store", "port"})
_PLACEHOLDER = re.compile(r"%[-#0 +]*\d*(?:\.\d+)?[sdrifx%]")
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def event_of(record: logging.LogRecord) -> str:
    given = record.__dict__.get("event")
    if isinstance(given, str) and given:
        return given
    template = record.msg if isinstance(record.msg, str) else type(record.msg).__name__
    return _NOT_WORD.sub("_", _PLACEHOLDER.sub(" ", template).lower()).strip("_")[:64] or "message"


def bare_traceback(exc_info) -> str:
    """Frames and exception types, never an exception's message."""
    _, value, _ = exc_info
    parts: list[str] = []
    seen: set[int] = set()
    while value is not None and id(value) not in seen:
        seen.add(id(value))
        frames = traceback.extract_tb(value.__traceback__)
        lines = [f'  File "{f.filename}", line {f.lineno}, in {f.name}' for f in frames]
        parts.insert(0, "\n".join(["Traceback (most recent call last):", *lines, type(value).__qualname__]))
        value = value.__cause__ or (None if value.__suppress_context__ else value.__context__)
    return "\n\nwhich led to:\n\n".join(parts)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, whatever the message holds: quotes and newlines cannot split a record."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        out = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "event": event_of(record),
            "msg": record.getMessage(),
        }
        for name in FIELDS:
            if name in record.__dict__:
                out[name] = record.__dict__[name]
        if record.exc_info and record.exc_info[1] is not None:
            out["exc"] = bare_traceback(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class ContentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a record that cannot format is not emitted
            metrics.LOG_DROPPED.labels(metrics.SERVER).inc()
            return False
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if _SHAPES.search(message) or (record.exc_text and _SHAPES.search(record.exc_text)):
            metrics.LOG_DROPPED.labels(metrics.SERVER).inc()
            return False
        return True


def dropped() -> float:
    return metrics.value("mcp_log_records_dropped_total")


def _uncaught(kind, value, tb) -> None:
    if issubclass(kind, KeyboardInterrupt):
        return
    logging.getLogger("exchange_mcp").critical(
        "uncaught exception", exc_info=(kind, value, tb), extra={"event": "uncaught"}
    )


LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def level_name() -> str:
    """`LOG_LEVEL` (unprefixed, as the platform sets it for every app), INFO when unset or unknown."""
    value = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    return value if value in LEVELS else "INFO"


def configure(level: int | None = None) -> None:
    unknown = os.environ.get("LOG_LEVEL", "INFO").strip().upper() not in LEVELS
    level = level if level is not None else getattr(logging, level_name())
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContentFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO and DEBUG; uvicorn's access log carries query strings. These
    # stay at WARNING whatever LOG_LEVEL says: their lower levels are content.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "spnego", "hpack"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, level))
    for own in ("uvicorn", "uvicorn.error"):  # no handlers of their own: their lines reach the JSON handler
        logging.getLogger(own).handlers[:] = []
        logging.getLogger(own).propagate = True
    logging.captureWarnings(True)  # warnings.warn would otherwise print plain text to stderr
    sys.excepthook = _uncaught
    if unknown:
        logging.getLogger("exchange_mcp").warning(
            "LOG_LEVEL is not a level name; using INFO", extra={"event": "log_level"}
        )
    threading.excepthook = lambda a: _uncaught(a.exc_type, a.exc_value, a.exc_traceback)
