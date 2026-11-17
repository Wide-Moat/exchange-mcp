# Live findings

What a real Exchange did, recorded when the live check ran. Each row names the unit test whose fake
answers in the measured shape. No host, address or person of the installation goes here.

| Operation | Exchange build | Observed | Unit test |
|---|---|---|---|
| (none yet) | | | |

## Verify on the first live run

| Question | Why it matters |
|---|---|
| `ResolveNames` for one's own short name returns the mailbox with `t:Contact/t:Alias` equal to the login | `auth/password.py` identifies the person by it |
| NTLM succeeds with Extended Protection on (channel binding sent) | `ews/transport.py` |
| `CreateItem` with `ReplyToItem` and `SaveOnly` returns the new draft's `ItemId` | `ews/mail.py::create_response` refuses otherwise |
| `item:Preview` and `item:Flag` are accepted in `FindItem` on 2016 | `ews/mail.py::LIST_FIELDS` |
| AQS `from:` and `received>=` work in `QueryString` | `tools/mail.py::list_messages` |
| `GetMailTips` answers for internal and external recipients | the draft preview |
| `ErrorServerBusy` carries `BackOffMilliseconds` as `t:Value Name=...` | `ews/client.py::_backoff` |
