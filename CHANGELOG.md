# Changelog

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
- Checked against Exchange Server 2016 CU23: every tool except `respond_to_event` (needs an invitation
  from someone else) and `save_automatic_reply` (left alone so a real reply's texts stay). What the live
  server did differently from the documentation is in `docs/live-findings.md`.
