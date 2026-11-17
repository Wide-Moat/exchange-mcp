# exchange-mcp

An MCP server that gives an AI assistant one person's on-premises Exchange mailbox — mail,
calendar, contacts, tasks — through Exchange Web Services, acting as that person and nothing more.

**Status: design.** The documents below are written before the code and bind it.

| Document | What it settles |
|---|---|
| [MANIFESTO.md](MANIFESTO.md) | the rules, each with what a violation looks like |
| [docs/requirements.md](docs/requirements.md) | the 21 tools, the EWS operations behind them, every refusal and its test |
| [docs/auth-modes.md](docs/auth-modes.md) | the two sign-in modes, their risks, and where the sealing key lives |
| [docs/architecture.md](docs/architecture.md) | components, request path, configuration, threats |
| [docs/code-review.md](docs/code-review.md) | what blocks a merge |
| [SECURITY.md](SECURITY.md) | reporting, and what a deployment must provide |

## Sign-in

| Mode | Needs | This server holds |
|---|---|---|
| `adfs` — **preferred** | Exchange 2019 CU13+ or Subscription Edition on every client-facing server; AD FS and Exchange configured by administrators | an AD FS token that reaches EWS only |
| `password` | any Exchange with EWS | the person's directory password, sealed; no second factor ([risks](docs/auth-modes.md#the-risks-of-password-and-what-holds-each)) |

The sealing key comes from Vault or OpenBao (recommended), or from a Kubernetes Secret generated at
deploy time. Never from an environment variable.

## Tools

| Area | Tools |
|---|---|
| account | `get_connection` |
| people | `list_people` |
| mail | `list_messages`, `get_message`, `save_mail_folder`, `delete_mail_folder`, `update_messages`, `save_draft`, `send_draft`, `save_automatic_reply` |
| calendar | `list_events`, `get_event`, `find_meeting_times`, `save_event`, `respond_to_event`, `cancel_event` |
| contacts | `save_contact`, `delete_contact` |
| tasks | `list_tasks`, `save_task`, `delete_task` |

Not here, by design: inbox rules, forwarding, delegates, permissions, other mailboxes,
impersonation, export, hard delete.
