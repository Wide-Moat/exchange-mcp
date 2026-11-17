# Requirements

The tools, what each calls in EWS, and every refusal. The rules behind them are in the
[manifesto](../MANIFESTO.md); numbers in brackets point there. A row in a **Must refuse** table names
the test that proves it; `tests/unit/test_gates.py::test_every_must_refuse_row_names_a_test_that_exists`
fails when a row names a test that does not exist. `scripts/mutation_check.py` removes the guards
behind most rows and requires their tests to fail.

EWS operations are requested with `RequestServerVersion` `Exchange2016` (the lowest version this
server supports; every operation used here exists there) and the person's time zone in
`TimeZoneContext`. Item ids returned to the model are EWS ids without their `ChangeKey`; the server
fetches the current `ChangeKey` itself before an update.

## Areas

| Area | Default | Tools |
|---|---|---|
| account | always on | `get_connection` |
| people | on | `list_people` |
| mail | on | `list_messages`, `get_message`, `save_mail_folder`, `delete_mail_folder`, `update_messages`, `save_draft`, `send_draft`, `save_automatic_reply` |
| calendar | on | `list_events`, `get_event`, `find_meeting_times`, `save_event`, `respond_to_event`, `cancel_event` |
| contacts | on | `save_contact`, `delete_contact` (contacts are found with `list_people`) |
| tasks | on | `list_tasks`, `save_task`, `delete_task` |

21 tools. An installation sets `EXCHANGE_MCP_AREAS`; an off area is not registered.

## Conventions shared by every tool

| Input | Accepted form | Server does |
|---|---|---|
| Person | display name, part of a name, or an address | `ResolveNames` against the address book and the person's contacts; ambiguity is an error listing up to 5 candidates with name, address, title |
| Mail folder | well-known name (`inbox`, `sent`, `drafts`, `deleted`, `archive`, `junk`, `outbox`), a display name, or a path `Inbox/Projects` | `FindFolder` (deep traversal, cached per request); two folders with one name is an error listing their paths |
| Time | `YYYY-MM-DD HH:MM` local to the mailbox time zone, or with an explicit offset | converts; output is local with offset |
| Duration | minutes | n/a |
| Cursor | opaque signed string from the previous page | wraps the EWS paging offset; a cursor from another tool, person or query is refused |
| Limit | 1–25, default 10 | values above 25 are clamped and the result says so |
| Ids | strings, as returned | a number is refused |

Every tool, on every call, refuses with a clear `isError`:

| Situation | Text tells the person |
|---|---|
| Token missing, expired, altered, or sealed under a key no longer held | HTTP 401 with `resource_metadata`; a client that does not re-authorize shows: sign in again at `<base>/signin` |
| EWS `401` (password changed, account disabled, AD FS token revoked) | sign in again; nothing was done |
| EWS `ErrorAccessDenied` | Exchange refused; which action did not happen |
| EWS `ErrorItemNotFound` on an id | the item does not exist or is not yours; the text never says which |
| `ErrorServerBusy` after back-off honouring `BackOffMilliseconds` | Exchange is busy; try again in N seconds |
| Any other EWS failure | that the action did not happen, with a correlation id; never reported as a success |

## Account

### `get_connection`
Who the person is (name, address), the sign-in mode (`adfs` or `password`, and for `password` one
sentence on what that means), mailbox time zone and working hours, areas on and off, whether an
automatic reply is on. Upstream: `ResolveNames` on the person's own address,
`GetUserOofSettings`, `GetUserConfiguration` (`WorkHours`) on the calendar.

## People and contacts

### `list_people(query, source=all, limit, cursor)`
Find colleagues, contacts and rooms. `source`: `all`, `directory` (the address book), `contacts`
(the person's own contacts), `rooms` (room lists and rooms). Returns name, address, title,
department, phone; each result says where it came from. Upstream: `ResolveNames`
(`ReturnFullContactData`; `SearchScope` `ActiveDirectoryContacts`, `ActiveDirectory`, or `Contacts` for
`source=contacts`), `GetRoomLists` / `GetRooms`.

| Must refuse | Test |
|---|---|
| an empty query (except `source=rooms`) | tests/unit/test_people.py::test_an_empty_query_is_refused |
| a distribution list expanded into members | tests/unit/test_people.py::test_distribution_lists_are_not_expanded |

### `save_contact(id?, name, emails, phones, company?, job_title?, notes?)`
Create or update a personal contact in the default Contacts folder. Upstream: `CreateItem` /
`UpdateItem` (`ConflictResolution=AutoResolve` after reading the current `ChangeKey`).

### `delete_contact(id)`
Moves one personal contact to Deleted Items. Upstream: `DeleteItem DeleteType=MoveToDeletedItems`.

| Must refuse | Test |
|---|---|
| more than 3 addresses or 4 phone numbers (EWS slots) | tests/unit/test_contacts.py::test_more_slots_than_exchange_has_are_refused |
| an id that is not a contact | tests/unit/test_contacts.py::test_a_non_contact_id_is_refused |
| any `DeleteType` other than MoveToDeletedItems | tests/unit/test_ews.py::test_only_move_to_deleted_items_is_sent |

## Mail

### `list_messages(kind=messages, folder?, query?, unread_only?, from?, since?, until?, has_attachments?, archive?, limit, cursor)`
Messages newest first, or with `kind=folders` the folder tree with unread and total counts. `query`
is the Exchange search syntax (`AQS`); `archive` searches the person's online archive when they have
one. Each message: id, folder, from, to (first 5 and a count), subject, received, preview (255
chars, untrusted block), unread, flag, categories, importance, has attachments, conversation.
Upstream: `FindItem` (`IndexedPageItemView`, `QueryString` or `Restriction`), `FindFolder`,
`ArchiveMsgFolderRoot` for the archive.

### `get_message(id, include_thread?, include_body=true)`
One message: headers, body reduced to text in an untrusted block (capped at 20,000 chars, says so),
attachments as `{name, size, type, link}` where `link` is a signed download link, inline images
listed, not linked. With `include_thread`, the other messages of the conversation (capped at 10,
newest first, previews only). Upstream: `GetItem` (`BodyType=HTML`, then the HTML pipeline's
reader), `GetConversationItems`, `GetAttachment` streamed through `/d/…`.

### `save_mail_folder(folder?, name?, parent?)`
No `folder` creates `name` under `parent` (default the mailbox root); with `folder`, renames it or
moves it under `parent`. Well-known folders cannot be renamed or moved. Upstream: `CreateFolder`,
`UpdateFolder`, `MoveFolder`.

### `delete_mail_folder(folder, user_confirmed)`
Moves a folder the person created, with everything in it, to Deleted Items. Upstream: `DeleteFolder
DeleteType=MoveToDeletedItems`.

### `update_messages(ids, action, folder?, categories?)`
`action`: `move`, `mark_read`, `mark_unread`, `flag`, `unflag`, `complete`, `categorize`,
`uncategorize`, `delete` (to Deleted Items; `move` brings them back). Up to 25 ids per call.
Upstream: `MoveItem`, `UpdateItem` (`IsRead`, `Flag`, `Categories`), `DeleteItem
DeleteType=MoveToDeletedItems`.

### `save_draft(id?, mode=new, message?, to, cc, bcc, subject?, body?, body_upload?, html?, attach, importance?)`
`mode`: `new`, `reply`, `reply_all`, `forward` (the last three need `message`). Short text goes in
`body`; a designed HTML letter is uploaded through the signed link returned when `body_upload` is
true (`curl -T letter.html`, UTF-8, at most 2 MB). `attach` names files in the sandbox; the result
carries one short-lived upload command per file. The result previews the draft (recipients
resolved, subject, the first 500 chars of text) with `{sanitized, warnings, size}` from the HTML
pipeline and the MailTips for its recipients (external, automatic reply on, over the size limit).
The draft is written to the person's Drafts folder and marked as made by this server (an extended
property). Upstream: `CreateItem MessageDisposition=SaveOnly`, `CreateItem` with
`ReplyToItem` / `ReplyAllToItem` / `ForwardItem`, `UpdateItem`, `CreateAttachment`, `GetMailTips`.

### `send_draft(id, user_confirmed)`
Sends a draft this server made, as saved. Irreversible. Upstream: `SendItem
SaveItemToFolder=true`.

### `save_automatic_reply(state, internal_text?, external_text?, external_audience=known, start?, end?, user_confirmed?)`
`state`: `off`, `on`, `scheduled`. Turning it on or scheduling it needs `user_confirmed` (everyone
who writes receives the text); turning it off does not. `external_audience`: `none`, `known`, `all`.
Upstream: `SetUserOofSettings` for the person's own address only.

| Must refuse | Test |
|---|---|
| `send_draft` without `user_confirmed` | tests/unit/test_mail.py::test_send_draft_needs_confirmation |
| `send_draft` of a draft this server did not make | tests/unit/test_mail.py::test_only_own_drafts_are_sent |
| `send_draft` of an item that is not a draft | tests/unit/test_mail.py::test_a_sent_message_is_not_sent_again |
| a send disposition anywhere but `send_draft` | tests/unit/test_ews.py::test_save_draft_never_sends |
| more than 20 recipients in one draft | tests/unit/test_mail.py::test_more_than_twenty_recipients_is_refused |
| a distribution list as a recipient | tests/unit/test_mail.py::test_a_distribution_list_recipient_is_refused |
| an 11th outward action within the bucket | tests/unit/test_core.py::test_the_bucket_refuses_the_eleventh_action |
| a recipient name that matches several people | tests/unit/test_mail.py::test_an_ambiguous_recipient_lists_candidates |
| HTML that keeps a script, an event handler or a `javascript:` URL after the pipeline | tests/unit/test_html.py::test_dangerous_html_does_not_survive |
| a relative URL in outgoing HTML | tests/unit/test_html.py::test_relative_urls_are_refused |
| an uploaded body over 2 MB or not UTF-8 | tests/unit/test_links.py::test_an_oversized_body_upload_is_refused |
| an attachment given as a path, a hidden file or an option (`../x`, `/etc/x`, `.env`, `-rf`) | tests/unit/test_mail.py::test_an_attachment_is_a_file_name_not_a_path |
| renaming, moving or deleting a well-known folder | tests/unit/test_mail.py::test_well_known_folders_stay |
| `delete_mail_folder` without `user_confirmed` | tests/unit/test_mail.py::test_deleting_a_folder_needs_confirmation |
| more than 25 ids in `update_messages` | tests/unit/test_mail.py::test_more_than_twenty_five_ids_is_refused |
| an automatic reply turned on without `user_confirmed` | tests/unit/test_mail.py::test_turning_on_an_automatic_reply_needs_confirmation |
| an automatic reply whose `end` is before its `start` | tests/unit/test_mail.py::test_an_automatic_reply_ending_before_it_starts_is_refused |
| a body read for the model that keeps hidden text | tests/unit/test_html.py::test_hidden_text_is_dropped_when_read |

## Calendar

### `list_events(start, end, query?, limit, cursor)`
Events of the person's own calendar in a window of at most 62 days, recurrences expanded. Each:
id, subject, start, end, location, organiser, my response, attendee count, online link if any,
recurring, private. Upstream: `FindItem` with `CalendarView` (paged by narrowing the window, since
`CalendarView` does not page), `query` filters subjects locally.

### `get_event(id, include_body=true)`
One event or occurrence: attendees with their responses (capped at 50, says so), body as text in
an untrusted block, recurrence described in words, attachments as signed links. Upstream:
`GetItem`.

### `find_meeting_times(people, start, end, duration?, rooms?, working_hours_only=true)`
Without `duration`, the busy times of each person (and room) in the window; with `duration`, up to
10 slots when everyone is free, best first. Upstream: `GetUserAvailability` (`FreeBusyViewOptions`
`Detailed` or `MergedOnly`, `SuggestionsViewOptions` with `MeetingDurationInMinutes`). At most 20
people and rooms, window at most 42 days (the EWS limit).

### `save_event(id?, subject, start, end?, duration?, attendees, rooms, location?, body?, repeat?, repeat_until?, repeat_count?, reminder_minutes?, show_as?, private?, user_confirmed?)`
Creates or updates an event in the person's own calendar. With attendees, sending the invitation
or the update needs `user_confirmed`; without attendees nothing leaves the mailbox. `repeat`:
`daily`, `weekdays`, `weekly`, `monthly`, `yearly`. Only the organiser changes a meeting.
Upstream: `CreateItem SendMeetingInvitations=SendToAllAndSaveCopy` or `SendToNone`, `UpdateItem
SendMeetingInvitationsOrCancellations=SendToChangedAndSaveCopy` or `SendToNone`.

### `respond_to_event(id, response, comment?, send_response=true)`
`response`: `accept`, `tentative`, `decline`. Answering one's own invitation needs no confirmation.
Upstream: `CreateItem` with `AcceptItem` / `TentativelyAcceptItem` / `DeclineItem`.

### `cancel_event(id, comment?, user_confirmed)`
The organiser cancels a meeting (attendees are told) or deletes an event without attendees.
Irreversible for attendees. Upstream: `CreateItem` with `CancelCalendarItem`; an event without
attendees goes to Deleted Items with `DeleteItem DeleteType=MoveToDeletedItems`.

| Must refuse | Test |
|---|---|
| an invitation, update or cancellation sent without `user_confirmed` | tests/unit/test_calendar.py::test_inviting_needs_confirmation |
| a change or cancellation by someone who is not the organiser | tests/unit/test_calendar.py::test_only_the_organiser_changes_a_meeting |
| more than 20 attendees and rooms | tests/unit/test_calendar.py::test_more_than_twenty_attendees_is_refused |
| a window longer than 62 days in `list_events`, 42 in `find_meeting_times` | tests/unit/test_calendar.py::test_windows_are_bounded |
| an end before the start, or both `end` and `duration` | tests/unit/test_calendar.py::test_an_impossible_time_is_refused |
| a repeat without an end (`repeat_until` or `repeat_count`) | tests/unit/test_calendar.py::test_a_repeat_must_end |
| `respond_to_event` on the person's own meeting | tests/unit/test_calendar.py::test_the_organiser_does_not_respond |

## Tasks

### `list_tasks(status=open, query?, due_before?, limit, cursor)`
Tasks from the default Tasks folder. `status`: `open`, `completed`, `all`. Upstream: `FindItem` on
`tasks`.

### `save_task(id?, subject, due?, start?, status?, importance?, body?, reminder?)`
`status`: `not_started`, `in_progress`, `completed`, `waiting`, `deferred`. Upstream: `CreateItem` /
`UpdateItem`.

### `delete_task(id)`
Moves a task to Deleted Items. Upstream: `DeleteItem DeleteType=MoveToDeletedItems`.

| Must refuse | Test |
|---|---|
| assigning a task to another person (task requests) | tests/unit/test_tasks.py::test_tasks_are_not_assigned_to_others |

## Contract (every tool)

| Must refuse | Test |
|---|---|
| a confirmation that is not the JSON boolean true | tests/unit/test_server.py::test_confirmation_must_be_the_boolean_true |
| an argument the tool does not have | tests/unit/test_server.py::test_an_unknown_argument_is_refused_before_upstream |
| a tool of an area that is off | tests/unit/test_server.py::test_an_off_area_is_not_published_and_cannot_be_called |
| a limit below 1 | tests/unit/test_core.py::test_a_limit_below_one_is_refused |
| a cursor from another tool, person or query | tests/unit/test_core.py::test_a_cursor_from_another_tool_or_person_is_refused |
| an id given as a JSON number | tests/unit/test_server.py::test_an_id_given_as_a_number_is_refused |
| an EWS operation outside the allowlist, from any code path | tests/unit/test_ews.py::test_an_operation_outside_the_allowlist_is_never_sent |
| an `ExchangeImpersonation` header, a `Mailbox` element for another address, or a request to another host | tests/unit/test_ews.py::test_requests_go_to_the_configured_mailbox_only |

## Sign-in, tokens and keys

| Must refuse | Test |
|---|---|
| key material given in the environment | tests/unit/test_keys.py::test_a_key_in_the_environment_stops_the_server |
| a key file readable by others or by a foreign group, or shorter than 32 bytes | tests/unit/test_keys.py::test_a_weak_or_open_key_file_stops_the_server |
| an altered token, or a token of another kind | tests/unit/test_oauth.py::test_an_altered_token_is_refused |
| a token sealed under a key id no longer held | tests/unit/test_oauth.py::test_a_token_of_a_retired_key_is_refused |
| an expired access or refresh token | tests/unit/test_oauth.py::test_tokens_expire |
| a code or refresh token of another client | tests/unit/test_oauth.py::test_a_code_belongs_to_its_client |
| a code used twice | tests/unit/test_oauth.py::test_a_code_is_used_once |
| a code exchange with a wrong PKCE verifier | tests/unit/test_http.py::test_a_wrong_pkce_verifier_is_refused |
| a redirect URI that is not https (except loopback) or differs from the registration | tests/unit/test_oauth.py::test_redirect_uris_are_exact |
| a 6th password attempt for one login within 15 minutes, or a 21st from one network | tests/unit/test_signin.py::test_password_attempts_are_limited |
| a login whose domain is not the configured one, before any attempt reaches Exchange | tests/unit/test_signin.py::test_a_login_of_another_domain_is_refused |
| one person counted as two because the login was typed in another form | tests/unit/test_signin.py::test_every_login_form_is_one_person |
| a sign-in answer that differs between an unknown login and a wrong password | tests/unit/test_signin.py::test_unknown_and_wrong_look_the_same |
| an EWS or AD FS address that is not https, or TLS verification turned off | tests/unit/test_config.py::test_tls_is_always_verified |
| `password` mode refresh tokens longer than 24 hours | tests/unit/test_config.py::test_password_mode_refresh_is_capped |
| a password, token or login in a log line or an exception message | tests/unit/test_observability.py::test_a_full_flow_logs_no_content |
| the sign-in page served without `Cache-Control: no-store`, a CSP, and `frame-ancestors 'none'` | tests/unit/test_signin.py::test_the_sign_in_page_is_not_cached_or_framed |
