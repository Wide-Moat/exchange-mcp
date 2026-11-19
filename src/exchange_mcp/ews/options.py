"""EWS for the person's Outlook on the web options: the signature and when OWA adds it.

OWA keeps them in a user configuration object named `OWA.UserOptions` on the mailbox's root folder (the
non-IPM root, DistinguishedFolderId `root`), as a dictionary. The keys this server reads, compared without
case: `signaturehtml`, `signaturetext`, `autoaddsignature` (new messages) and `autoaddsignatureonreply`
(replies and forwards; the same options `Set-MailboxMessageConfiguration` writes). A mailbox whose owner
never opened OWA has no such object, and Exchange answers ErrorItemNotFound.
"""

from __future__ import annotations

from dataclasses import dataclass

from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.soap import M, T, find, findall, text

OWA_OPTIONS = "OWA.UserOptions"


@dataclass(frozen=True)
class OwaSignature:
    html: str
    text: str
    on_new: bool
    on_reply: bool


def _flag(value: str) -> bool:
    return value.strip().lower() == "true"


async def owa_options(ews: Ews) -> dict[str, str]:
    """The dictionary of OWA.UserOptions, keys lower-cased, each value its first `t:Value` as text."""
    messages = await ews.call(
        M.GetUserConfiguration(
            M.UserConfigurationName(T.DistinguishedFolderId(Id="root"), Name=OWA_OPTIONS),
            M.UserConfigurationProperties("Dictionary"),
        )
    )
    out: dict[str, str] = {}
    for entry in findall(find(messages[0], "m:UserConfiguration"), "t:Dictionary/t:DictionaryEntry"):
        key = text(entry, "t:DictionaryKey/t:Value").strip().lower()
        if key and key not in out:
            out[key] = text(entry, "t:DictionaryValue/t:Value")
    return out


async def owa_signature(ews: Ews) -> OwaSignature:
    o = await owa_options(ews)
    return OwaSignature(
        html=o.get("signaturehtml", ""),
        text=o.get("signaturetext", ""),
        on_new=_flag(o.get("autoaddsignature", "")),
        on_reply=_flag(o.get("autoaddsignatureonreply", "")),
    )
