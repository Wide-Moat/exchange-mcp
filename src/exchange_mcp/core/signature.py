"""The person's signature on a draft, added the way Outlook on the web adds it.

- New message: added when OWA's `autoaddsignature` is on.
- Reply, reply to all, forward: added when `autoaddsignatureonreply` is on, after the new text and above
  the quoted original (Exchange puts NewBodyContent there).
- An edit that replaces the body: added again only when the draft was saved with a signature (its
  marker property) and the new body does not already carry the signature's text. The old body went with
  its signature, so this restores it rather than repeats it.
- An edit that leaves the body: never; the saved signature stays where it is.
- Any body that already carries the signature's text (a model that copied the preview back): not added.

An HTML draft takes `signaturehtml` (else `signaturetext`, escaped); a text draft takes `signaturetext` (else
`signaturehtml` as text). Either way the signature passes the same pipeline as a body (`prepare_outgoing`):
it is mailbox content and is treated as untrusted. Nothing about the signature ever fails a draft: no
signature, no OWA options (ErrorItemNotFound), an Exchange error, or HTML the pipeline refuses, and the
draft is saved without it, with one line in the preview saying why.

The server-wide switch is `EXCHANGE_MCP_SIGNATURE` (`owa`, the default, or `off`); a call can leave it out
with `signature=false`. The options are read at most once per call and never cached beyond it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.core.html import InlineImage, html_to_text, prepare_outgoing, text_to_html
from exchange_mcp.ews import options as eo
from exchange_mcp.ews.client import Ews

log = logging.getLogger("exchange_mcp.signature")

Kind = Literal["new", "reply", "edit"]

ADDED = "added from Outlook on the web"
ADDED_AGAIN = "added again from Outlook on the web: the new body replaced the old one, signature included"
OFF = "not added: signatures are switched off on this server"
NOT_WANTED = "not added: signature=false"
NO_OPTIONS = "not added: no signature is set in Outlook on the web"
UNREADABLE = "not added: Exchange did not return the Outlook on the web signature settings"
OFF_NEW = "not added: Outlook on the web is set not to add it to new messages"
OFF_REPLY = "not added: Outlook on the web is set not to add it to replies and forwards"
REFUSED = "not added: the signature's HTML did not pass the checks"
KEPT = "kept as saved: the body was not changed"
ALREADY = "kept: the new body already carries it"
UNSIGNED = "not added: the draft was saved without one"


@dataclass
class Signature:
    """What to append (`html`, empty for none), its images, and the preview's one line."""

    note: str
    html: str = ""
    inline_images: list[InlineImage] = field(default_factory=list)
    sanitized: list[str] = field(default_factory=list)

    @property
    def added(self) -> bool:
        return bool(self.html)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def carries(body_html: str, signature_html: str) -> bool:
    """True when the body already holds the signature: the no-duplicate check. By its text; a signature
    that is only an image (no text, no alt) by its image sources, every one of them present in the body."""
    sig = _norm(html_to_text(signature_html))
    if sig:  # whole words only: a short signature ("Ann") is not found inside "planning"
        return re.search(rf"(?<!\w){re.escape(sig)}(?!\w)", _norm(html_to_text(body_html))) is not None
    images = set(_SRC.findall(signature_html))
    return bool(images) and images <= set(_SRC.findall(body_html))


def inline_name(img: InlineImage) -> str:
    """The attachment name an inline image is saved under; its content id is derived from its bytes."""
    return f"{img.content_id}.{img.content_type.split('/')[-1]}"


def to_attach(body_images: list[InlineImage], sig: Signature, attached: set[str]) -> list[InlineImage]:
    """The inline images still to attach: the body's and the signature's, each once, and none the draft
    already has. An edit replaces the body but keeps the attachments, so a signature put back must not
    attach its logo again."""
    out: list[InlineImage] = []
    seen = set(attached)
    for img in [*body_images, *sig.inline_images]:
        if inline_name(img) not in seen:
            seen.add(inline_name(img))
            out.append(img)
    return out


async def for_draft(ews: Ews, *, setting: str, wanted: bool, kind: Kind, html_draft: bool) -> Signature:
    """The signature for a draft being created (`new`, `reply`) or whose body an edit replaces (`edit`)."""
    if setting != "owa":
        return Signature(OFF)
    if not wanted:
        return Signature(NOT_WANTED)
    try:
        owa = await eo.owa_signature(ews)
    except ExchangeError as e:
        if e.key == "not_found":
            return Signature(NO_OPTIONS)
        log.info("signature options unreadable: %s", e.key)
        return Signature(UNREADABLE)
    except Exception as e:  # noqa: BLE001 - nothing about a signature may fail the draft
        log.warning("signature options unreadable: %s", type(e).__name__)
        return Signature(UNREADABLE)
    if kind == "new" and not owa.on_new:
        return Signature(OFF_NEW)
    if kind == "reply" and not owa.on_reply:
        return Signature(OFF_REPLY)
    if html_draft:
        source = owa.html if owa.html.strip() else (text_to_html(owa.text) if owa.text.strip() else "")
    else:
        plain = owa.text if owa.text.strip() else (html_to_text(owa.html) if owa.html.strip() else "")
        source = text_to_html(plain) if plain.strip() else ""
    if not source:
        return Signature(NO_OPTIONS)
    prepared = prepare_outgoing(source)
    if prepared.errors:
        return Signature(REFUSED + ": " + "; ".join(prepared.errors))
    if not prepared.html.strip():
        return Signature(NO_OPTIONS)
    note = ADDED_AGAIN if kind == "edit" else ADDED
    if prepared.sanitized:
        note += "; cleaned: " + ", ".join(prepared.sanitized)
    return Signature(
        note,
        html=prepared.html,
        inline_images=prepared.inline_images,
        sanitized=prepared.sanitized,
    )


def marker(sig: Signature) -> bool | None:
    """What the draft's signature marker becomes after its body was written: True when the body carries
    the signature, False when the person or the server left it out, None (unchanged) when it could not be
    read, so a later edit can still put it back."""
    if sig.added or sig.note == ALREADY:
        return True
    if sig.note in (NOT_WANTED, OFF):
        return False
    return None


def settle(body_html: str, sig: Signature) -> tuple[str, Signature]:
    """The body to save and the signature as it ended: appended, or kept out because the body already
    carries it (a model that copied the preview back into body must not sign the letter twice)."""
    if sig.added and carries(body_html, sig.html):
        return body_html, Signature(ALREADY)
    return append(body_html, sig), sig


def append(body_html: str, sig: Signature) -> str:
    """The body with the signature after it, separated by an empty line as OWA does."""
    if not sig.added:
        return body_html
    return f"{body_html}<div><br></div><div>{sig.html}</div>"
