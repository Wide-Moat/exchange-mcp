# exchange-mcp

An MCP server that gives an AI assistant one person's on-premises Exchange mailbox (mail,
calendar, contacts, tasks) through Exchange Web Services, acting as that person with no further rights.

| Your mailbox lives in | Use |
|---|---|
| Exchange Server in your own data centre (2016, 2019, Subscription Edition) | **this server** (Exchange Web Services) |
| Microsoft 365 / Exchange Online | [microsoft-365-mcp](https://github.com/Wide-Moat/microsoft-365-mcp) (Microsoft Graph); Microsoft turns EWS off in Exchange Online |
| Both, during a migration | both, one per mailbox location |

Free and open source (Apache-2.0). **Status: 0.2.0**, tested against Exchange Server 2016 CU23; [`CHANGELOG.md`](CHANGELOG.md) lists what each release holds. We wrote the documents below before the code, and the code follows them.

| Document | What it settles |
|---|---|
| [MANIFESTO.md](MANIFESTO.md) | the rules, each with what a violation looks like |
| [docs/requirements.md](docs/requirements.md) | the 21 tools, the EWS operations behind them, every refusal and its test |
| [docs/auth-modes.md](docs/auth-modes.md) | the two sign-in modes, their risks, and where the sealing key lives |
| [docs/architecture.md](docs/architecture.md) | components, request path, configuration, threats |
| [docs/admin-guide.md](docs/admin-guide.md) | how to deploy it and what to ask your Exchange administrators |
| [docs/code-review.md](docs/code-review.md) | what blocks a merge |
| [SECURITY.md](SECURITY.md) | reporting, and what a deployment must provide |

## Sign-in

| Mode | Needs | This server holds | Checked against a live server |
|---|---|---|---|
| `adfs` (**preferred**) | Exchange 2019 CU13+ or Subscription Edition on every client-facing server; AD FS and Exchange configured by administrators | an AD FS token that reaches EWS only | **not yet**: unit tests against a fake AD FS only ([what to check](docs/live-findings.md#not-yet-run-live)) |
| `password` | any Exchange with EWS | the person's directory password, sealed; no second factor ([risks](docs/auth-modes.md#the-risks-of-password-and-what-holds-each)) | yes, Exchange Server 2016 CU23 ([findings](docs/live-findings.md)) |

The sealing key comes from Vault or OpenBao (recommended), or from a Kubernetes Secret the deployer
owns, created once. Never from an environment variable.

## Install

```sh
scripts/new-sealing-key.sh exchange-mcp exchange-mcp-key   # once; prints only the key id
helm upgrade --install exchange-mcp oci://ghcr.io/wide-moat/charts/exchange-mcp -n exchange-mcp \
  --set publicUrl=https://mcp.corp.example \
  --set exchange.ewsUrl=https://mail.corp.example/EWS/Exchange.asmx \
  --set auth.password.loginDomain=corp.example \
  --set key.existingSecret=exchange-mcp-key \
  --set 'state.sentinel.hosts={redis-sentinel:26379}'   # or state.url=redis://…; or replicaCount=1
```

Two replicas by default, sharing the key and a Redis or Valkey store; probes and Prometheus metrics on
port 9090; JSON logs. The tool surface is a contract, pinned in
[`tool-surface.lock.json`](tool-surface.lock.json): a change to a tool's name, arguments or annotations
fails CI until the lock is updated on purpose.

[`charts/exchange-mcp/README.md`](charts/exchange-mcp/README.md) lists every value and how the key
reaches the server.

## Tools

| Area | Tools |
|---|---|
| account | `get_connection` |
| people | `list_people` |
| mail | `list_messages`, `get_message`, `save_mail_folder`, `delete_mail_folder`, `update_messages`, `save_draft`, `send_draft`, `save_automatic_reply` |
| calendar | `list_events`, `get_event`, `find_meeting_times`, `save_event`, `respond_to_event`, `cancel_event` |
| contacts | `save_contact`, `delete_contact` |
| tasks | `list_tasks`, `save_task`, `delete_task` |

Excluded by design: inbox rules, forwarding, delegates, permissions, other mailboxes,
impersonation, export, hard delete.

## Security review

| Question | Answer |
|---|---|
| AI models used by the server | none: it serves tools to the assistant the organisation already runs |
| Outbound connections | the configured EWS address (https, CA verified); AD FS in `adfs` mode; the secrets manager with `key.provider: vault`; the state store (Redis or Valkey). Nothing on the internet: Autodiscover is never used, no telemetry |
| Data in | the person's login and password (`password` mode) or an AD FS token (`adfs`), tool arguments from the assistant, files uploaded through short-lived links |
| Data out | the person's own mail, calendar, contacts and tasks, to the assistant that called the tool; drafts and sends to Exchange only after `user_confirmed=true` |
| What the server stores | nothing about the person: the credential travels sealed (AES-GCM) inside the person's own tokens |
| What the state store holds, and for how long | HMAC-named counters and marks only: redeemed sign-in codes (6 min), sign-in attempt windows (15 min), the send allowance (≤1 min to refill), running uploads (3 min). No credential, token, address or content |
| Logs | JSON, keys and outcomes only; a filter drops any line shaped like an address, a token or a sealed blob, and counts it (`mcp_log_records_dropped_total`) |
| Sign-in protection | attempt limits per login and per client network, shared by every replica; identical answers for unknown and wrong; refused while the store is unreachable |
| Report a defect | [SECURITY.md](SECURITY.md) |
