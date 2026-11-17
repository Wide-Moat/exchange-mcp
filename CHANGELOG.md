# Changelog

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
