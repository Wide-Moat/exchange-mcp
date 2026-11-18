# Changelog

## 0.1.5

| Area | Fixed |
|---|---|
| Sender filter | `sender=<own address>` found 1 of 5 sent messages: Exchange 2016 matches an address only on new messages, not replies or forwards. The address is searched together with its owner's name as the address book writes it, then held to the exact address |
| People | every phone number of an entry is shown, business first, not only the first |

## 0.1.4

| Area | Fixed |
|---|---|
| People | a personal contact's phone was missing: phone numbers are an indexed property and are now asked for by kind (business, mobile, home, other) |

## 0.1.3

Fixes from the second QA run on Exchange Server 2016 CU23.

| Area | Fixed |
|---|---|
| Drafts | an element marked `hidden` went out as visible text (the sanitiser dropped the attribute and kept the content); such a draft is now refused with the reason |
| Mail lists | sender and recipient names come marked as someone else's text, one marker for a row's recipients (manifesto 29) |
| Sender filter | `sender=<address>` found nothing on Exchange 2016: the address is searched unquoted, then held to the exact address; a one-word name is searched unquoted |
| People | a personal contact shows its own name, title, company and phone, read from the contact itself |

## 0.1.2

| Area | Fixed |
|---|---|
| Sign-in | Chrome could not sign in: the page sent `Referrer-Policy: no-referrer`, under which Chrome posts the form with `Origin: null`, and the origin check refused it. The page now sends `same-origin`; a null or foreign origin is still refused |
| Sign-in | a refused form logs which check failed (origin, missing cookie, older page), never a value |

## 0.1.1

Fixes from a QA run of 131 cases on Exchange Server 2016 CU23.

| Area | Fixed |
|---|---|
| Automatic reply | turning it off failed every time: Exchange requires both reply texts in each request, so the saved texts are sent back with the new state |
| Sign-in | parallel calls sometimes answered "sign in again": NTLM legs of different calls shared pooled connections (fixed in 0.1.0; measured 14 of 30 before, 0 of 30 after) |
| Archive | `archive=true` found nothing: the archive's mail sits in a copy of the mailbox's tree under its localized names |
| Previews | built from the body without hidden text, no longer Exchange's own preview (which kept it); `MaximumBodySize` is in characters |
| Attachments | uploading again through the same link no longer attaches the file twice |
| Text from others | organiser, location and names are marked or reduced to one bounded line |
| Mail lists | `folder` on each message, `to` as names with a count, SMTP instead of X.500 addresses, sender filter by address, thread entries with previews, folder subtrees |
| Calendar | an occurrence describes its series; replies and forwards quote times in the person's zone |
| People | a contact with several addresses is one row under its own name; candidates carry their title |
| Messages | why a folder delete needs confirmation; an automatic reply already past is refused with its reason; times returned with their offset |

## 0.1.0

The first release: one person's on-premises Exchange mailbox for an AI assistant, through EWS.

| Area | Tools |
|---|---|
| account | `get_connection` |
| people | `list_people` (address book, personal contacts, rooms) |
| mail | `list_messages`, `get_message`, `save_mail_folder`, `delete_mail_folder`, `update_messages`, `save_draft`, `send_draft`, `save_automatic_reply` |
| calendar | `list_events`, `get_event`, `find_meeting_times`, `save_event`, `respond_to_event`, `cancel_event` |
| contacts | `save_contact`, `delete_contact` |
| tasks | `list_tasks`, `save_task`, `delete_task` |

- Sign-in: `password` (NTLM with channel binding) and `adfs` (confidential client, `private_key_jwt`, PKCE;
  needs Exchange 2019 CU13+ or Subscription Edition). The `password` mode's costs are listed in
  `docs/auth-modes.md`.
- Its own OAuth 2.1 server, stateless: client ids, codes and tokens are sealed with AES-256-GCM. The key
  comes from Vault or OpenBao transit, a file generated at deploy, or memory; a key in the environment
  stops the server.
- Sending is two steps, and anything other people see needs `user_confirmed=true` and draws from a bucket
  of 10. Deletion moves to Deleted Items. Rules, forwarding, delegates, impersonation and export do not
  exist.
- Sign-in page in English, Russian, Spanish, German and French, chosen by the browser.
- Checked against Exchange Server 2016 CU23 in `password` mode; `adfs` mode is covered by unit tests
  against a fake AD FS and has not run against a live one. Every tool was checked except `respond_to_event` (needs an invitation
  from someone else) and `save_automatic_reply` (left alone so a real reply's texts stay). What the live
  server did differently from the documentation is in `docs/live-findings.md`.
