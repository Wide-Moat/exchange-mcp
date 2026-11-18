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
| `GetUserAvailability` with a UTC zone whose two rules are the same transition | 2016 CU23 | fault "The specified time zone isn't valid"; two distinct zero-bias transitions work | tests/unit/test_calendar.py::test_the_utc_zone_has_two_distinct_transitions |
| `FindItem` on Contacts with `Contains` on `contacts:DisplayName` and `EmailAddress1-3` | 2016 CU23 | matches a whole address only, never part of a name; `ResolveNames` with `SearchScope=Contacts` finds both | tests/unit/test_people.py::test_contacts_come_from_the_contacts_folder_with_ids |
| Meetings: create, weekly repeat read back, invitation, move, cancel; free/busy and slots | 2016 CU23 | as built | tests/unit/test_calendar.py |
| `DeleteItem` of a task with `AffectedTaskOccurrences=SpecifiedOccurrenceOnly` | 2016 CU23 | `ErrorCannotDeleteTaskOccurrence`; `AllOccurrences` works | tests/unit/test_tasks.py::test_a_task_is_deleted_whole |
| Contacts and tasks: create, find, complete, delete | 2016 CU23 | as built after the two fixes above | tests/unit/test_contacts.py, test_tasks.py |
| `SetUserOofSettings` without `InternalReply` and `ExternalReply` | 2016 CU23 | `ErrorInvalidUserOofSettings`: both are required in every request; turning the reply off sends the saved texts back | tests/unit/test_review_fixes.py::test_turning_the_automatic_reply_off_keeps_its_texts |
| NTLM legs of parallel calls on one shared connection pool | 2016 CU23, two front ends | 14 of 30 parallel calls answered 401 ("sign in again"); one connection per NTLM call: 0 of 30 | tests/unit/test_ews.py::test_ntlm_calls_run_on_a_client_of_their_own |
| `GetItem` of an occurrence of a series | 2016 CU23 | no `Recurrence` on the occurrence; the master (`RecurringMasterItemId`) has it | tests/unit/test_calendar.py::test_an_occurrence_describes_its_series |
| `ResolveNames` over Contacts for a contact with several addresses | 2016 CU23 | one resolution per address, each named by that address | tests/unit/test_people.py::test_a_contact_with_three_addresses_is_one_row_with_its_name |
| `ItemShape/MaximumBodySize` | 2016 CU23 | counted in characters, not kilobytes: 64 returned 64 characters of HTML | tests/unit/test_qa_mail.py::test_a_preview_is_built_from_the_body_without_hidden_text |
| Online archive filled by an archive policy | 2016 CU23 | mail sits in a top-level copy of the mailbox's folders under their localized names; the distinguished `archiveinbox` is empty | tests/unit/test_qa_mail.py::test_the_archive_inbox_is_the_copy_of_the_mailbox_tree |
| `FindItem` on tasks with a restriction on `task:Status` | 2016 CU23 | `ErrorInvalidValueForProperty`; `task:IsComplete` works instead | tests/unit/test_tasks.py::test_list_tasks_filters_open_tasks |
| AQS `from:` for one's own address | 2016 CU23 | `from:addr` unquoted finds one's own mail; `from:"addr"` and `from:"Display Name"` find none | tests/unit/test_qa_mail.py::test_a_sender_address_is_searched_unquoted_and_matched_exactly |
| `ResolveNames` over Contacts for a personal contact | 2016 CU23 | `t:Name` is the address; no display name, title, company or phone; `GetItem` on the contact's id has them | tests/unit/test_people.py::test_a_contact_reads_its_name_title_and_phone_from_the_item |
| HTML written with `<div hidden>` through `save_draft` | 2016 CU23 | arrived as a visible div: the sanitiser drops the attribute and kept the text; such a draft is now refused | tests/unit/test_html.py::test_an_element_marked_hidden_is_refused_not_sent_visible |

## Not yet run live

| What | Why it is open | Unit tests that stand in |
|---|---|---|
| `adfs` mode, end to end: redirect to AD FS, `private_key_jwt` accepted by the token endpoint, the Bearer token accepted by EWS, refresh | needs Exchange 2019 CU13+ or Subscription Edition and an AD FS configured for it; no such installation was available | tests/unit/test_adfs.py (7 tests, fake AD FS) |
| `respond_to_event` | needs an invitation from another person | tests/unit/test_calendar.py::test_respond_sends_the_answer |
| Times in the header Exchange quotes into a reply or forward, in the person's zone | needs a sent reply | tests/unit/test_calendar.py::test_requests_carry_the_persons_time_zone |

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
