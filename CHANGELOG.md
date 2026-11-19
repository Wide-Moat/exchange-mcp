# Changelog

## 0.2.0

Many replicas, JSON logs, Prometheus metrics: the same runtime as the other MCP servers of this family.

| Area | Changed |
|---|---|
| Replicas | any number: redeemed sign-in codes, sign-in attempt windows, the send bucket and upload marks live in a shared store, Redis or Valkey 7+, standalone (`EXCHANGE_MCP_STATE_URL`) or behind Sentinel (`EXCHANGE_MCP_STATE_SENTINELS`, one host is enough), with or without a password (`EXCHANGE_MCP_STATE_PASSWORD_FILE`, files only). Without a store the state stays in the process and the chart allows one replica, as before |
| Store contents | HMAC-named counters and marks, every key with a TTL; the HMAC keys are derived from the sealing keys, and a redeemed code is marked under every key of the ring, so it stays redeemed through a key rotation. No credential, token, address or content |
| Store down | sign-in (503, "try again in a minute"), code redemption, sending and uploads refuse; reading goes on; `/readyz` stays ready; `mcp_state_store_up 0`. A store out of memory under `noeviction` refuses the same way |
| Sign-in limit per network | believed only from `EXCHANGE_MCP_TRUSTED_PROXIES`; unset, the per-network limit is off. Before, behind an ingress every person shared one network: 20 failures locked everyone out |
| Logs | every line of every process (server, uvicorn, the init container, a refused start, an uncaught exception, a warning) is one JSON object with `ts`, `level`, `logger`, `event`, `msg`; tracebacks carry frames, never an exception's message; `LOG_LEVEL`. The init container printed plain text before |
| Metrics | new: `mcp_*` with `server="exchange-mcp"` on port 9090 (`EXCHANGE_MCP_METRICS_PORT`), never the public port: requests in flight, requests and duration by route group, tool calls and duration, Exchange requests, sign-ins, OAuth and guard refusals, file links, the state store, dropped log records, `mcp_build_info`. The documents claimed metrics and `/readyz` that did not exist |
| Probes | `/readyz` (the key seals and opens) and `/healthz`, on both ports; the chart probes the metrics port |
| Chart | `replicaCount: 2`, RollingUpdate (`maxSurge: 1`, `maxUnavailable: 0`; `Recreate` without a store), PodDisruptionBudget, metrics on a ClusterIP Service of their own, soft spread over nodes, optional HPA (off; CPU, and in-flight requests through prometheus-adapter) and ServiceMonitor, `state.*`, `trustedProxies`, `logLevel`, `tmp.*`. Refuses more than one replica without a store, and `key.provider: memory` with more than one |
| Ingress | routes `/mcp`, `/.well-known/`, `/authorize`, `/token`, `/register`, `/signin`, `/static/`, `/files/`, `/adfs/`; never `/` |
| Sealing key | stable: created once in a Secret the deployer owns, so deploys keep every session; rotation is a separate step, documented with and without sign-outs |
| Downloads | at most four attachments held in memory at once |
| Image | `ARG BASE_IMAGE` (digest-pinned default); built once for amd64 and arm64, each architecture scanned and started, and that manifest list pushed; no `latest` tag any more: pin `0.2.0`, `0.2` or the digest |
| CI | the store contract suite runs against real Valkey and Redis, standalone and behind Sentinel; a skipped test fails the run. `scripts/chart-e2e.sh` runs three replicas against Valkey behind Sentinel and a stand-in Exchange with NTLM over TLS, and a second job runs its replica checks against 0.1.7, where each must fail |
| Upgrade | set `state.url` or `state.sentinel.hosts`, or `replicaCount: 1`; keep `key.existingSecret`; set `trustedProxies` to the ingress controllers' networks |

## 0.1.7

| Area | Added |
|---|---|
| Chart | `oci://ghcr.io/wide-moat/charts/exchange-mcp`: one Deployment and Service, optional Ingress, no ServiceAccount token, read-only root, all capabilities dropped |
| Sealing key | CI writes a new key into the Secret with `scripts/new-sealing-key.sh`, which prints only the key id. An init container is the only one that mounts the Secret; it copies the key to a memory volume, 0400, owned by the server's user. A container restart keeps sessions; a new key signs everyone out once |
| Process | on Linux the server marks itself not dumpable before reading any setting: a `kubectl exec` shell of the same user cannot read its memory or environment through `/proc` |
| CI | the chart is linted, checked against the Kubernetes schemas, made to refuse missing values, and installed in a kind cluster where `scripts/chart-e2e.sh` checks each of the above |
| Replicas | one replica is the supported count, and the chart refuses more: used sign-in codes and attempt counters live in the process, so a second pod would accept a sign-in code twice. The documents said any number |

## 0.1.6

| Area | Fixed |
|---|---|
| Sign-in | an NTLM handshake that Exchange did not answer with a challenge failed the call with "Exchange refused the request" and no next step (QA, fifth run: the first three calls after a restart). The handshake, which carries no body and no password, is tried once more on a new connection; if it fails again the error says nothing was sent and to try again or tell the administrator |
| Logs | a failed handshake logs which step failed and the HTTP status beside the reference; before, the log said only `refused: failed` |

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
