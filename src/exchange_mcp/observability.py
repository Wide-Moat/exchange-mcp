"""Logs carry keys, never content (manifesto rule 40).

Code logs a fixed event and a key. As a second line of defence, a filter drops any record whose final
text holds something shaped like an address, a JWT, an NTLM or bearer header, or a sealed blob, and
counts the drop, so a regression shows as a counter instead of a leak.
"""

from __future__ import annotations

import logging
import re

_SHAPES = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+"  # an address
    r"|eyJ[\w-]{10,}\.[\w-]{10,}"  # a JWT
    r"|\b(?:NTLM|Negotiate|Bearer)\s+[A-Za-z0-9+/=._-]{16,}"  # an Authorization value
    r"|\b[\w-]{1,32}\.[A-Za-z0-9_-]{40,}"  # a sealed blob
)

DROPPED = {"count": 0}


class ContentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a record that cannot format is not emitted
            DROPPED["count"] += 1
            return False
        if _SHAPES.search(message) or (record.exc_text and _SHAPES.search(record.exc_text)):
            DROPPED["count"] += 1
            return False
        return True


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ContentFilter())
    handler.setFormatter(
        logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}', style="%")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO; uvicorn's access log carries query strings.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "spnego"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
