"""The one error type a tool raises, and the sentences a person reads.

`str()` of an error is its key alone. Tracebacks and log lines print `str()`, and the parameters
(names, subjects, counts) are the person's data, so they reach the model through the result and never
the log.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# key -> sentence. Every sentence names the next step, or says what did not happen.
MESSAGES: dict[str, str] = {
    "reconnect": (
        "Exchange is not signed in on this connector, or the sign-in ended (a changed password, a revoked "
        "grant, or a server restart). Sign in again: reconnect the connector, or open {signin}. Nothing was done."
    ),
    "area_off": "The {area} tools are switched off on this server.",
    "forbidden": "Exchange does not let you do this. {outcome}",
    "not_found": "It does not exist, or it is not yours to see. Find it again with the tool that lists it.",
    "busy": "Exchange is busy right now. Try again in {seconds}. Nothing was done.",
    "failed": "Exchange refused the request ({code}). {outcome} Reference {ref}.",
    "uncertain": (
        "Exchange failed while handling this, so it may or may not have happened. Check before trying again. "
        "Reference {ref}."
    ),
    "partial": (
        "Exchange applied {done} of {total} and refused the rest, so part of this happened. List the items "
        "again to see where they are before trying again. Reference {ref}."
    ),
    "ambiguous": 'More than one {kind} matches "{query}". Ask which one is meant: {candidates}.',
    "unresolved": 'No {kind} matches "{query}". {where}',
    "confirm": (
        "Other people see this at once. Ask the person to confirm{target}, then call again with "
        "user_confirmed=true. {outcome}"
    ),
    "too_many": "At most {limit} {what} per action; this has {count}. Nothing was done.",
    "rate": (
        "Too many actions that other people see, in a short time. The allowance is the person's: {capacity} "
        "at once, then one every {every} seconds. Wait {seconds}. {then}"
    ),
    "not_own_draft": (
        "That is not a draft this connector made, or it was already sent. Save it with save_draft first. "
        "Nothing was sent."
    ),
    "not_organiser": "Only the organiser changes or cancels a meeting. Nothing was changed.",
    "too_large": "It is too large: {size} against a limit of {limit}. Nothing was done.",
    "invalid": "{reason}",
    "unsupported": "{reason}",
    "unexpected": "Something went wrong on our side; check whether the change happened before trying again.",
}

WHERE = {
    "person": "list_people finds people by part of a name; an e-mail address always works.",
    "folder": "list_messages with kind=folders lists the folders.",
    "room": "list_people with source=rooms lists the rooms.",
}


def _sentence_params(key: str, params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if "seconds" in out:
        out["seconds"] = f"{out['seconds']} second" + ("" if out["seconds"] == 1 else "s")
    if key == "confirm":
        out.setdefault("outcome", "Nothing was sent.")
        out["target"] = f" ({out['target']})" if out.get("target") else ""
    if key in ("forbidden", "failed"):
        out.setdefault("outcome", "Nothing was changed.")
    if key == "rate":
        out.setdefault("then", "Nothing was sent.")
    if key == "unresolved":
        out["where"] = WHERE.get(str(out.get("kind")), "Check the spelling.")
    if key == "reconnect":
        out.setdefault("signin", "the connector's sign-in page")
    return out


class ExchangeError(ToolError):
    """A refusal or failure the person should read. `isError` is set by the SDK."""

    def __init__(self, key: str, **params: Any) -> None:
        if key not in MESSAGES:
            raise KeyError(f"unknown error key {key!r}")
        self.key = key
        self.params = params
        super().__init__(key)
        self._text = MESSAGES[key].format(**_sentence_params(key, params))

    def __str__(self) -> str:
        return self.key

    @property
    def text(self) -> str:
        return self._text


def invalid(reason: str) -> ExchangeError:
    return ExchangeError("invalid", reason=reason)


def unsupported(reason: str) -> ExchangeError:
    return ExchangeError("unsupported", reason=reason)
