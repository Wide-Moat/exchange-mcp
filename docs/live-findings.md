# Live findings

What a real Exchange did, recorded when the live check ran. Each row names the unit test whose fake
answers in the measured shape. No host, address or person of the installation goes here.

| Operation | Exchange build | Observed | Unit test |
|---|---|---|---|
| NTLM sign-in, `ResolveNames` for one's own short name identifies the mailbox | 2016 CU23 | sign-in completes, tokens issued | tests/unit/test_signin.py::test_a_full_sign_in_issues_tokens_that_carry_the_person |
| `FindItem` with `item:Preview`, `item:Flag`, sort and paging; AQS `QueryString` | 2016 CU23 | accepted | tests/unit/test_mail.py |
| `FindFolder` deep traversal; `CalendarView`; `GetRoomLists` | 2016 CU23 | accepted | tests/unit/test_calendar.py, test_people.py |
| `save_draft` → `send_draft` to an external address | 2016 CU23 | draft marked as ours, external recipient flagged, `"yes"` refused, sent once, a second send of the same draft refused, newest item in Sent Items | tests/unit/test_mail.py::test_send_draft_sends_the_saved_draft_once |
| AQS search for a message sent seconds ago | 2016 CU23 | not found yet: the search index lags by minutes; newest-first listing finds it | (live probe reads Sent Items without a query) |
| `FindItem` on tasks with a restriction on `task:Status` | 2016 CU23 | `ErrorInvalidValueForProperty`; `task:IsComplete` works instead | tests/unit/test_tasks.py::test_list_tasks_filters_open_tasks |

## Verify on the first live run

| Question | Why it matters |
|---|---|
| `ResolveNames` for one's own short name returns the mailbox with `t:Contact/t:Alias` equal to the login | `auth/password.py` identifies the person by it |
| NTLM succeeds with Extended Protection on (channel binding sent) | `ews/transport.py` |
| `CreateItem` with `ReplyToItem` and `SaveOnly` returns the new draft's `ItemId` | `ews/mail.py::create_response` refuses otherwise |
| Refresh-token exchange on the live server | the probe refreshes its saved token on the next run |
| `item:Preview` and `item:Flag` are accepted in `FindItem` on 2016 | `ews/mail.py::LIST_FIELDS` |
| AQS `from:` and `received>=` work in `QueryString` | `tools/mail.py::list_messages` |
| `GetMailTips` answers for internal and external recipients | the draft preview |
| `ErrorServerBusy` carries `BackOffMilliseconds` as `t:Value Name=...` | `ews/client.py::_backoff` |
