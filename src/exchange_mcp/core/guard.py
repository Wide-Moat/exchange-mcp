"""The one gate in front of everything other people see: the person's word, the size of the action, and
one bucket per person shared by every kind of action.

The bucket lives in this replica's memory: with N replicas a person's allowance is at most N times the
burst. That is the documented cost of keeping no shared store.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from exchange_mcp.core.errors import ExchangeError

MAX_RECIPIENTS = 20
CAPACITY = 10
REFILL_PER_SECOND = 10 / 60
MAX_PEOPLE_TRACKED = 50_000

UNDONE = {
    "mail": "Nothing was sent.",
    "invite": "No one was invited and nothing was changed.",
    "cancel": "The meeting was not cancelled.",
    "auto_reply": "The automatic reply was not changed.",
    "folder": "Nothing was deleted.",
}
RATE_THEN = {"mail": "Nothing was sent and the draft is kept: call send_draft again later; do not save it again."}


@dataclass
class _Bucket:
    tokens: float
    at: float


class Guard:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def _take(self, person: str, kind: str) -> int:
        now = self._clock()
        if len(self._buckets) >= MAX_PEOPLE_TRACKED and person not in self._buckets:
            full = [p for p, b in self._buckets.items() if b.tokens + (now - b.at) * REFILL_PER_SECOND >= CAPACITY]
            for p in full:
                del self._buckets[p]
        b = self._buckets.get(person)
        if b is None:
            b = self._buckets[person] = _Bucket(CAPACITY, now)
        b.tokens = min(CAPACITY, b.tokens + (now - b.at) * REFILL_PER_SECOND)
        b.at = now
        if b.tokens < 1:
            wait = math.ceil((1 - b.tokens) / REFILL_PER_SECOND)
            raise ExchangeError(
                "rate",
                seconds=wait,
                capacity=CAPACITY,
                every=round(1 / REFILL_PER_SECOND),
                then=RATE_THEN.get(kind, UNDONE.get(kind, "Nothing was done.")),
            )
        b.tokens -= 1
        return int(b.tokens)

    def confirmed(self, *, kind: str, user_confirmed: bool | None, target: str | None = None) -> None:
        """The person's word alone, for actions that do not draw on the bucket (deleting one's own folder)."""
        if user_confirmed is not True:
            raise ExchangeError("confirm", target=target, outcome=UNDONE.get(kind, "Nothing was done."))

    def check(
        self, person: str, *, kind: str, user_confirmed: bool | None, recipients: int = 0, target: str | None = None
    ) -> dict:
        """Call before the EWS request. Returns the receipt the tool puts in its result."""
        self.confirmed(kind=kind, user_confirmed=user_confirmed, target=target)
        if recipients > MAX_RECIPIENTS:
            raise ExchangeError("too_many", limit=MAX_RECIPIENTS, what="recipients", count=recipients)
        return {"sends_left": self._take(person, kind)}
