# Code review

What a reviewer (a person, or `ocr review` driven by [`.opencodereview/rule.json`](../.opencodereview/rule.json))
looks for in every change. Each item points at the [manifesto](../MANIFESTO.md) rule it protects.
A finding is fixed, or rejected with its reason written in the review. No finding is dropped without one.

## Blocking

| # | Look for | Rule |
|---|---|---|
| B1 | HTTP or a SOAP request built outside `ews/`. The other HTTP is named and nowhere else: AD FS in `auth/adfs.py`, the secrets manager in `keys/vault.py`, the client factory in `app.py`, the link routes' responses in `links/routes.py` | 41 |
| B2 | An argument selecting a mailbox, user or server; `ExchangeImpersonation`; a `Mailbox` element for an address other than the person's own; a URL built from a person's input | 16 |
| B3 | Key material read from `os.environ`; a key, password or token in a log call, an exception message, a URL or a result | 19, 20, 40 |
| B4 | A credential, session or draft kept in a database, a file or a shared store | 19 |
| B5 | `verify=False`, an `http://` upstream, or a setting that produces either | 22 |
| B6 | Any code path that sends mail, invites, updates or cancels with attendees, or turns on an automatic reply without `user_confirmed is True` and a bucket draw before the EWS call | 24–26 |
| B7 | `DeleteType` other than `MoveToDeletedItems`; `EmptyFolder`; `MessageDisposition` other than `SaveOnly` outside `send_draft` | 23, 24 |
| B8 | Inbox rules, forwarding, delegates, permissions, other mailboxes, export, `SyncFolderItems`, subscriptions, public folders, in a tool, an EWS method or a route; a new operation in the allowlist | 28 |
| B9 | A tool result built from an EWS object without a projection; `ChangeKey`, `ItemClass` or a `ResponseCode` reaching the model | 8 |
| B10 | A list without a cap of 25, an `offset` argument, a truncation that does not say so | 9 |
| B11 | Bytes or base64 in a tool result | 10 |
| B12 | An exception returned as a successful result; a result shaped `{"error": …}` | 11 |
| B13 | An annotation that contradicts the code | 12 |
| B14 | Outgoing HTML that skips a stage of the pipeline | 32 |
| B15 | A reply or forward that replaces the body instead of `ReplyToItem` / `ReplyAllToItem` / `ForwardItem` | 33 |
| B16 | A signed-link or seal check with `==`; a signer that works with an empty key; a payload field outside the signature or the AAD | 19, 35 |
| B17 | A file read whole into memory | 36 |
| B18 | Sign-in without the attempt limit, or with answers that differ for an unknown login | 21 |
| B19 | Documentation, a page or a log line presenting `password` as equivalent to `adfs` | 18 |
| B20 | A new behaviour or refusal without a test that fails without it | 38 |
| B21 | A host, domain, address or name of a real installation or person | 43 |

## Must answer

- **What does the model see?** The tool's description and a real (redacted) result.
- **What does it cost?** The catalogue's token count before and after.
- **What can the person not undo?** Every effect visible to other people and how it is gated.
- **What does Exchange actually do?** For any new EWS call, the live finding or the recorded fixture.
- **What fails, and how does the person learn of it?** The error text for each refusal.
- **What does a thief get?** For any change near tokens, keys or sign-in: what a stolen token, a
  stolen key and a compromised pod each open after the change.

## Tests a reviewer expects to see

- A negative test for every refusal, asserting `isError`, the text, and that nothing was written
  (`ews.writes() == []`).
- Contract tests green: names, descriptions, annotations, budget, `docs/tools.md` in sync.
- Recorded EWS fixtures for new calls; nothing that talks to a real Exchange outside the `live`
  marker.
- For anything streaming: a fake that reproduces the stream closing early.
