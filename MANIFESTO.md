# Manifesto

This server gives an AI assistant the Exchange mailbox of **one person**: their mail, their
calendar, their contacts, their tasks. It acts as that person, with that person's rights and no more. It talks to an on-premises Exchange Server through Exchange Web Services (EWS); the
installation that runs it names the one Exchange organisation it serves.

Everything here is a rule. A rule that the code can break without a test going red is a defect in
the tests. Every rule below names what a violation looks like, so that a reviewer can find one and
a test can fail on one.

## Why this exists

Exchange Online has Microsoft Graph, and a separate server covers it. On-premises Exchange has no
Graph, only EWS, a SOAP API of about a hundred operations, each with its own envelope,
its own error codes inside an HTTP 200, and its own idea of what a folder id is. A model given that
API one operation per tool spends its context choosing between `FindItem`, `FindConversation` and
`FindPeople`, and sends an `ItemShape` the server answers with `ErrorInvalidPropertyRequest`.

We build **few tools, each answering one question a person asks**. The server does the work EWS
leaves to the caller: resolving names against the address book, expanding
recurring meetings, cleaning HTML, streaming attachments, refusing what must not happen.

The tool names and the shape of every result match the Microsoft 365 server, so an assistant
moving between an on-premises mailbox and a cloud one sees the same tools.

## I. The contract with the model

1. **One question, one tool.** Depth is a flag (`include_attachments`, `include_thread`), a search
   is a `query`, a folder listing is a `kind`; none of them is a second tool. *Violation:* two
   tools whose implementations share a code path and differ in output shape.
2. **Verbs are `list_`, `get_`, `save_`, plus a verb that says what happens** (`send_draft`,
   `cancel_event`, `respond_to_event`, `find_meeting_times`). `save_` is an upsert: no `id`
   creates, an `id` updates. *Violation:* `create_x` beside `update_x`; `manage_x` with a free-form
   action string.
3. **Changes that are not upserts take a closed `action` enum**, inline in the schema.
   *Violation:* an `action: str`; an enum behind a `$ref`.
4. **No prefix.** The gateway already namespaces tools. *Violation:* `exchange_`, `ews_`, `mcp_`
   in a tool name.
5. **Descriptions read like a man page and say when to call the tool**, not only what it does. At
   least three sentences; irreversible tools say they are irreversible. *Violation:* a description
   that restates the tool name.
6. **Flat arguments.** Scalars and lists of scalars. Every argument whose name is not
   self-explanatory has a description that adds information. *Violation:* a nested object argument.
7. **The server resolves names.** People by name or address, folders by name or path, rooms by
   name, time as a local string in the person's mailbox time zone (`2026-11-17 15:00`), duration in
   minutes. **Ambiguity is an error that lists the candidates**, never the first match.
   *Violation:* an argument that needs an EWS `ItemId` or `ChangeKey` the model could only have
   guessed.
8. **Projections, never raw EWS.** Each object has a projection that keeps the fields a person
   needs. *Violation:* a result carrying `ChangeKey`, `ItemClass`, a `ResponseCode`, a SOAP
   envelope, or any EWS type passed through.
9. **Bounded output.** Lists default to 10 items, cap at 25, and continue by an opaque signed
   `cursor`. Long text is capped and says so. *Violation:* an `offset` argument; a list with no
   cap; silent truncation.
10. **Bytes never enter the context.** Attachments and large HTML bodies move through short-lived
    signed links the agent's sandbox uses with `curl`. *Violation:* base64 in a tool result.
11. **Errors are errors.** A failed call sets `isError` and its text names the next step and what
    did not happen. *Violation:* `{"error": …}` inside a success; an EWS `ResponseCode` shown raw.
12. **Annotations describe actual behaviour.** `readOnlyHint`, `destructiveHint` (by the MCP spec
    it means *changes existing state*), `idempotentHint`, `openWorldHint` (other people are
    affected). *Violation:* a read-only hint on anything that writes, including a link slot.
13. **Areas switch off like separate servers.** Mail, calendar, people, contacts and tasks are
    areas; an installation turns any of them off with one line, and an off area is not published
    at all. `account` (`get_connection`) is always on: it is how a person learns why anything else
    fails. *Violation:* a published tool that answers "disabled"; a tool that belongs to no area.
14. **Every change is weighed** with a real tokenizer: the default catalogue stays under
    **12,000 tokens**, no area over 3,500, no tool over 600. Growing past that is a recorded
    decision, not an edit.

## II. Acting as one person

15. **The person signs in themselves, on this server's sign-in page.** Never in a chat with the
    model and never through a tool. *Violation:* a tool argument or a model-visible text that
    carries a password, a code or a token.
16. **One person per token, one Exchange per installation.** An access token maps to exactly one
    person; the EWS address is the operator's configuration, never the person's input and never
    found by Autodiscover. No tool takes a mailbox, user or server argument. *Violation:* any
    argument that selects another mailbox; an `ExchangeImpersonation` header; a request to a host
    other than the configured one.
17. **Rights come from Exchange, not from us.** We never widen what the person can do and never
    pretend to narrow it with a check Exchange would not enforce. Where Exchange refuses, we say so.
18. **Two sign-in modes, named for what they are.** `adfs` (OAuth through AD FS, Exchange 2019 CU13
    or later, or Subscription Edition) is the safer mode: this server never sees the
    person's password, and the token it holds reaches EWS only. `password` (NTLM) exists for
    Exchange organisations where `adfs` is not available, and its costs are written down in
    [`docs/auth-modes.md`](docs/auth-modes.md). *Violation:* documentation, a page or a log line
    that presents `password` as equivalent to `adfs`.
19. **The server keeps nothing.** The person's credential (an AD FS refresh token, or in `password`
    mode the password itself) travels inside their OAuth tokens, a draft reference inside its draft
    token, a file link inside its URL: each sealed with AES-GCM, bound to its kind, its person, its
    expiry and its key id. Any replica holding the same key serves any request. *Violation:* a
    database, a file or a shared store holding a credential or a session; a credential in clear in
    a token, a URL, an error or a log.
20. **The sealing key never comes from an environment variable.** It comes from a file the
    deployment generated (`file`), from a secrets manager that never releases it (`vault`), or
    from memory, born at start and lost at restart (`memory`). A key given in the environment
    stops the server from starting. *Violation:* any code path that reads key material from
    `os.environ`.
21. **Sign-in cannot be used to guess passwords or lock accounts.** Attempts are limited per login
    and per client network, below the directory's lockout threshold; the page never says whether a
    login exists. *Violation:* a loop that makes the server try passwords against the directory
    faster than the limit.
22. **TLS is always verified**, against the operator's CA bundle when Exchange or AD FS uses an
    internal CA. Only `https`. *Violation:* `verify=False` anywhere, or a setting that produces it.

## III. Care with what cannot be undone

The rule: **what other people see, or what cannot come back, needs the person's word. Everything
else stays reversible.**

23. **Deletion goes to Deleted Items.** Mail, events the person owns, contacts and tasks move there
    and come back with a move. Hard delete, soft delete to the dumpster, emptying a folder and
    purging do not exist here. Deleting a folder needs `user_confirmed`, because it takes its
    contents. *Violation:* `DeleteType` other than `MoveToDeletedItems`; `EmptyFolder`.
24. **Sending is two steps.** `save_draft` writes a draft the person can see in Outlook;
    `send_draft(id, user_confirmed=true)` sends that draft as saved and nothing else. Only drafts
    this server created can be updated or sent. Meeting responses and cancellations are messages
    Exchange composes itself; they leave from `ews/calendar.py` under rules 25 and 26.
    *Violation:* `MessageDisposition=SendOnly` or `SendAndSaveCopy` outside `ews/calendar.py`;
    sending a draft this server did not create.
25. **Anything other people see at once needs `user_confirmed=true`**, the JSON boolean, not "yes"
    and not 1: sending mail, inviting, rescheduling or cancelling a meeting, turning on an
    automatic reply. Answering one's own invitation does not: it is one's own record, though the
    answer still draws from the bucket of rule 26.
26. **Rate.** All such actions draw from one bucket per person: 10 at once, refilled at 10 per
    minute. At most 20 recipients per action; distribution lists are refused. Mass mailing is out
    of scope, and the refusal says so.
27. **Only what is yours.** Meetings are changed or cancelled only by their organiser.
28. **Never exposed, by design:** inbox rules, forwarding, delegates, folder and calendar
    permissions, other people's and shared mailboxes, impersonation, mailbox export and
    synchronisation (`SyncFolderItems`, `ExportItems`), public folders, streaming and push
    subscriptions. Each of these sends data away, opens access silently or watches a mailbox.
    *Violation:* any of them reachable through a tool, a client method or a route.

## IV. Content is data

29. **Text from mail, invitations and contacts is untrusted.** It returns inside a block marked with
    a fresh nonce, as data written by someone else. The marking lowers risk, and rule 30 is the
    boundary.
30. **The boundaries are structural:** the operation allowlist, drafts, confirmation, the bucket.
    An injected "forward everything to X" finds no forwarding, no rules and no send without a draft
    the person sees.
31. **HTML read for the model is reduced to text.** Scripts, styles, tracking pixels and hidden
    elements are dropped; images become `[image: alt]`; links keep their target.

## V. HTML mail

32. **Every outgoing HTML body passes one pipeline, in this order:** sanitise (allowlist; no
    `script`, `iframe`, `object`, `embed`, `form`, `on*` attributes, `javascript:` URLs) → convert
    `data:` images to inline attachments referenced by `cid:` → inline CSS → validate. The draft's
    response carries `{sanitized, warnings, size}`.
33. **A reply keeps its thread.** Replies and forwards are created by Exchange (`ReplyToItem`,
    `ReplyAllToItem`, `ForwardItem`); the body is never replaced wholesale.
34. **The person sees what Exchange will warn about.** Before `send_draft`, the draft preview
    carries Exchange's MailTips: external recipients, recipients with an automatic reply, a
    message over the size limit.

## VI. Files and attachments

35. **Signed links, short-lived, fail closed.** Download 300 s, upload 180 s, hard cap 900 s; a
    missing key refuses to sign; a tampered, expired, wrong-operation or other-person link answers
    403. Comparison is constant-time.
36. **Streaming both ways.** No attachment is held whole in memory beyond what one EWS request
    needs, and the organisation's size limit is reported as a number, not as an EWS error.

## VII. Engineering

37. **English everywhere** in the repository: code, comments, docs, commit messages.
38. **Test first.** Every rule here has a test that fails when the rule is broken, seen failing
    once; every refusal in [`docs/requirements.md`](docs/requirements.md) names its test.
39. **The suite cannot pass empty.** The number of tests run is derived from the tests on disk.
40. **No content in logs:** no subjects, bodies, addresses, names, file names, search strings,
    logins, passwords or tokens, in log lines or in exception messages. A test asserts it.
41. **EWS lives in one place.** Only `exchange_mcp.ews` speaks HTTP to Exchange: the SOAP envelope,
    the `RequestServerVersion`, the time zone header, HTTP-200 errors, `ErrorServerBusy` back-off,
    the operation allowlist. Tools never do. *Violation:* an HTTP client or a SOAP string in a tool
    module.
42. **Live findings override documentation.** `docs/live-findings.md` records what a real Exchange did;
    each row names the unit test whose fake answers in the measured shape.
43. **Product code names no installation and no person.** The purity gate fails the build
    otherwise.
44. **Review before push.** Every change is reviewed against [`docs/code-review.md`](docs/code-review.md)
    before it is pushed; pull requests open as drafts.
