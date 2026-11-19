"""The one gate in front of everything other people see: the person's word, the size of the action, and
one bucket per person shared by every kind of action.

The bucket lives in the shared state store (`state.store`), so a person has one allowance however many
replicas serve them. A store that does not answer refuses the action: nothing is sent without the count.
"""

from __future__ import annotations

from exchange_mcp import metrics
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.state.store import StateStore, StoreFull, StoreUnavailable, wait_seconds

MAX_RECIPIENTS = 20
CAPACITY = 10
REFILL_PER_SECOND = 10 / 60

UNDONE = {
    "mail": "Nothing was sent.",
    "invite": "No one was invited and nothing was changed.",
    "cancel": "The meeting was not cancelled.",
    "auto_reply": "The automatic reply was not changed.",
    "folder": "Nothing was deleted.",
}
RATE_THEN = {"mail": "Nothing was sent and the draft is kept: call send_draft again later; do not save it again."}


class Guard:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def _take(self, person: str, kind: str) -> int:
        try:
            taken, tokens = await self._store.take_token("send", person, CAPACITY, REFILL_PER_SECOND)
        except StoreFull:
            metrics.guard_refused("busy")
            raise ExchangeError("busy", seconds=60) from None
        except StoreUnavailable:
            metrics.guard_refused("state_unavailable")
            raise ExchangeError("state_unavailable", outcome=UNDONE.get(kind, "Nothing was done.")) from None
        if not taken:
            metrics.guard_refused("rate")
            raise ExchangeError(
                "rate",
                seconds=wait_seconds(tokens, REFILL_PER_SECOND),
                capacity=CAPACITY,
                every=round(1 / REFILL_PER_SECOND),
                then=RATE_THEN.get(kind, UNDONE.get(kind, "Nothing was done.")),
            )
        return int(tokens)

    def confirmed(self, *, kind: str, user_confirmed: bool | None, target: str | None = None) -> None:
        """The person's word alone, for actions that do not draw on the bucket (deleting one's own folder)."""
        if user_confirmed is not True:
            metrics.guard_refused("confirm")
            if kind == "folder":  # nobody else sees it: the reason to ask is what it takes with it (QA D17)
                raise ExchangeError("confirm_delete", target=target)
            raise ExchangeError("confirm", target=target, outcome=UNDONE.get(kind, "Nothing was done."))

    async def check(
        self, person: str, *, kind: str, user_confirmed: bool | None, recipients: int = 0, target: str | None = None
    ) -> dict:
        """Call before the EWS request. Returns the receipt the tool puts in its result."""
        self.confirmed(kind=kind, user_confirmed=user_confirmed, target=target)
        if recipients > MAX_RECIPIENTS:
            metrics.guard_refused("too_many")
            raise ExchangeError("too_many", limit=MAX_RECIPIENTS, what="recipients", count=recipients)
        return {"sends_left": await self._take(person, kind)}

    async def spend(self, person: str, *, kind: str) -> dict:
        """An action others see that needs no confirmation (answering one's own invitation): no word asked,
        but it still draws on the same allowance."""
        return {"sends_left": await self._take(person, kind)}
