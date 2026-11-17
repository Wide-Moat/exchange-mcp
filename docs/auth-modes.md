# Sign-in modes and keys

This server signs a person in one of two ways, chosen by the installation (`EXCHANGE_MCP_AUTH_MODE`).
Both put the same OAuth 2.1 server in front of MCP clients, so a client cannot tell them apart;
they differ in what this server holds on the person's behalf.

> **`adfs` is the safer mode.** In it this server never sees the person's password, and
> the token it holds reaches Exchange Web Services and nothing else. It needs Exchange Server 2019
> CU13 or later, or Exchange Server Subscription Edition, on every server that accepts client
> connections, and configuration by the Exchange and AD FS administrators. `password` exists for
> organisations where that is not available. Its costs are listed below in full, so the
> organisation's security team decides with them in view.

## How each mode works

```
adfs
  person ──▶ this server /authorize ──▶ AD FS sign-in page (password, MFA: AD FS policy)
                                            │ code
  this server ◀─────────────────────────────┘ exchanges it as a confidential client
             (private_key_jwt) for an access token to EWS and a refresh token
  → the AD FS refresh token is sealed into this server's own refresh token
  → each call: refresh if needed → Authorization: Bearer → EWS

password
  person ──▶ this server /authorize ──▶ this server's sign-in page (login, password)
  → the password is checked once against EWS (NTLM), then sealed into this server's tokens
  → each call: open the token in memory → NTLM → EWS → the password is dropped
```

| | `adfs` | `password` |
|---|---|---|
| This server sees the password | never | at sign-in and, in memory, on every call |
| What a stolen credential opens | EWS for one person, until the AD FS token expires or the app is disabled | the person's directory account: mail, and whatever else accepts that password |
| Multi-factor authentication | whatever AD FS requires | none: NTLM does not ask |
| Revoking one person | disable the person, or revoke their AD FS grants | change or reset their password |
| Revoking everyone | disable this application in AD FS | rotate this server's key |
| The person changes their password | nothing breaks | the next call answers 401 and the person signs in again |
| Exchange version | 2019 CU13+ or SE on every client-facing server | any version with EWS |
| Administrators | AD FS application, Exchange authentication policy and auth server | a DNS name, a certificate, network access to EWS |

## The login a person types

People type their login three ways: `name`, `DOMAIN\name`, `name@domain`. The operator sets the
one form this Exchange accepts, and the server turns each of the three into it, so the same login
works however it is typed.

| `EXCHANGE_MCP_LOGIN_FORMAT` | `EXCHANGE_MCP_LOGIN_DOMAIN` | Typed | Sent to Exchange |
|---|---|---|---|
| `upn` | `corp.example` | `name`, `CORP\name`, `name@corp.example` | `name@corp.example` |
| `down_level` | `CORP` | `name`, `CORP\name`, `name@corp.example` | `CORP\name` |

A login with a domain other than the configured one is refused before any attempt reaches
Exchange, so it cannot count against someone else's lockout. The person's identity inside the
tokens is the normalised login, so one person has one identity whichever way they typed it.

## The risks of `password`, and what holds each

| Risk | What holds it | What is left |
|---|---|---|
| This server sees directory passwords | they are opened only in memory, for one request, and never written to a log, a result, an error or a URL (tested) | code running inside the pod sees the passwords of people using it at that moment |
| A password opens more than mail | only an allowlist of EWS operations is sent; rules, forwarding, delegates and other mailboxes do not exist here | outside this server, a leaked password is a leaked account |
| No second factor | n/a | none; this is the main reason `adfs` is preferred |
| Guessing passwords, locking accounts | attempts limited per login (5 in 15 minutes) and per network (20); an unknown login and a wrong password answer alike | set the limit below the directory's lockout threshold |
| A refresh token carries the password for a long time | refresh tokens in `password` mode live 12 hours by default, 24 at most; rotating the key ends all of them | none |
| A stolen token used elsewhere | tokens are sealed with AES-GCM and bound to kind, person, client, expiry and key id; without the key they are noise | a token stolen together with the key opens the password |
| TLS to an internal CA | the operator's CA bundle; verification cannot be turned off; `https` only | none |

## The sealing key

Every token, draft reference, cursor and file link is sealed with one key. The place where the key lives
decides what a thief has to take.

| `EXCHANGE_MCP_KEY_PROVIDER` | Where the key is | Pod restart | Replicas | Use it when |
|---|---|---|---|---|
| `vault` | a Vault or OpenBao transit key: it never leaves the secrets manager, and every unseal is in its audit log | nothing happens | any | **recommended** whenever the organisation runs a secrets manager |
| `file` | a Kubernetes Secret **generated at deploy time** by the chart's pre-sync job, mounted read-only as a file, readable only by this server's ServiceAccount | nothing happens | any | **the required minimum** without a secrets manager |
| `memory` | 32 random bytes born at start, in memory only | everyone signs in again | 1 | a single-replica pilot that accepts a sign-in after every restart |

Rules, enforced at start:

- **Never an environment variable.** It shows in `/proc/<pid>/environ`, in `kubectl exec env` and
  in the Deployment. A key in the environment stops the server.
- **Never typed by a person, never committed, never in CI variables.** The chart generates it; no
  human sees it.
- **A key file** must be at least 32 bytes and readable only by this server's user and its own group (0400, or 0440 with the pod's fsGroup).
- **Rotation** is a new key id. Tokens sealed under a key id the server no longer holds answer 401;
  clients re-authorise, the person signs in once.
- **Generating a new key on every deploy** is allowed; everyone then signs in again after each
  deploy. The chart's default keeps the key until the operator rotates it.

With any provider, code running inside the pod can ask for unseals while it runs. No provider
prevents that; `vault` makes each unseal visible.

## When a token stops working

Rotation, a restart under `memory`, a changed password or a revoked AD FS grant all end the same
way:

1. The call answers HTTP 401 with `WWW-Authenticate: Bearer error="invalid_token",
   resource_metadata="<base>/.well-known/oauth-protected-resource"`. Clients that implement MCP
   authorization re-authorise on their own and show the person a sign-in.
2. For clients that do not, the tool error tells the person to sign in again at `<base>/signin`.
   The link carries no parameter, no token and no login.

## What the administrators do for `adfs`

Written out in [`docs/admin-guide.md`](admin-guide.md) (to be written with the `adfs` mode):
an authentication policy that keeps Modern Auth on EWS only, an AD FS server application with key
authentication and this server's callback, a Web API for Exchange whose `scp` rule carries
`EWS.AccessAsUser.All` and `offline_access` and nothing for ActiveSync, Exchange's trust in AD FS
(`New-AuthServer -Type ADFS`), and turning off automatic forwarding to external domains.
