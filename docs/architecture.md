# Architecture

## Shape

```
MCP client (gateway, chat UI, desktop client)
   │  Streamable HTTP /mcp, OAuth 2.1 (MCP authorization: metadata, dynamic registration, PKCE)
   ▼
exchange-mcp :8000 ── /mcp  /authorize  /token  /register  /signin  /.well-known/*  /healthz /readyz /metrics
exchange-mcp :8001 ── /d/<link>  /u/<link>   (signed attachment and body links; the only port sandboxes reach)
   │
   ├─ oauth/      the authorization server; every artefact is a sealed blob, nothing is stored
   ├─ auth/       sign-in modes: password (NTLM) and adfs (confidential client to AD FS)
   ├─ keys/       key providers: vault, file, memory
   ├─ tools/      one module per area; no HTTP, no SOAP
   ├─ core/       resolve, project, cursor, guard (confirmation + bucket), html, time, errors
   ├─ links/      signed links and their streaming routes
   └─ ews/        the only module that speaks to Exchange: SOAP, allowlist, back-off, errors
   ▼
Exchange EWS  https://<operator-configured host>/EWS/Exchange.asmx     (Autodiscover: never)
AD FS         https://<operator-configured host>/adfs/oauth2/...       (adfs mode only)
```

Stateless: two replicas with the same key serve the same person. Per-replica state is limited to
what is safe to lose: the sign-in attempt counters, the send bucket and the used-code set. The code documents each
of these where it lives, with what a second replica changes.

## Request path

```
POST /mcp  Authorization: Bearer <sealed access token>
  → oauth: open(kind=access) → Person{key, login, mode, credential, exp}   (401 on anything wrong)
  → registry: tool of an enabled area? arguments exactly the schema?
  → tool: resolve names → guard (user_confirmed, bucket) → ews.<operation>(...)
  → ews: allowlist check → SOAP envelope → transport (NTLM or Bearer) → parse → EwsError | result
  → tool: project → bound → untrusted blocks → result
```

## Components

| Module | Owns | Never |
|---|---|---|
| `ews/` | envelopes, `RequestServerVersion`, `TimeZoneContext`, the operation allowlist, `ResponseClass`/`ResponseCode` mapping, `ErrorServerBusy` back-off, the transport for both modes | knows about tools or the model |
| `auth/password.py` | checks a password once with a cheap EWS call (`ResolveNames` on oneself), builds an NTLM transport per request | stores, logs or returns the password |
| `auth/adfs.py` | authorization code + PKCE against AD FS, `private_key_jwt` client authentication, refresh | accepts a token AD FS did not issue for EWS |
| `keys/` | one interface, `seal(kind, payload, ttl)` / `open(kind, blob)`, key ids, rotation | reads key material from the environment |
| `oauth/` | metadata, registration (the `client_id` is a sealed registration), codes (5 min, used once per replica), access (1 h), refresh (`adfs` 30 days, `password` 12 h) | a database |
| `core/guard.py` | `user_confirmed is True`, the per-person bucket, recipient caps | lets a tool reach EWS before both pass |
| `core/html.py` | outgoing pipeline (sanitise → `cid:` → inline CSS → validate) and the reader (HTML → text, hidden text dropped) | n/a |
| `links/` | HMAC-signed, expiring, person-bound links; streaming in both directions | holds a file whole in memory |
| `observability.py` | key-only log lines with an allowlist of fields; a filter that drops any record with an address, a JWT or a token shape; Prometheus counters | content |

## Configuration (operator)

| Setting | Meaning |
|---|---|
| `EXCHANGE_MCP_EWS_URL` | the one EWS endpoint, `https` only |
| `EXCHANGE_MCP_CA_BUNDLE` | path to the CA bundle for Exchange and AD FS when they use an internal CA |
| `EXCHANGE_MCP_AUTH_MODE` | `adfs` or `password` |
| `EXCHANGE_MCP_LOGIN_FORMAT` | `password` mode: how a login is sent to Exchange, `upn` (`name@domain`) or `down_level` (`DOMAIN\name`) |
| `EXCHANGE_MCP_LOGIN_DOMAIN` | `password` mode: the domain added when a person types a bare `name` (the NetBIOS name for `down_level`, the UPN suffix for `upn`) |
| `EXCHANGE_MCP_LOGIN_ALSO` | `password` mode: the other spelling accepted exactly (the UPN suffix for `down_level`, the NetBIOS name for `upn`) |
| `EXCHANGE_MCP_ADFS_*` | issuer, client id, signing key file, EWS resource (`adfs` mode) |
| `EXCHANGE_MCP_KEY_PROVIDER` | `vault`, `file` or `memory` ([auth-modes](auth-modes.md#the-sealing-key)) |
| `EXCHANGE_MCP_BASE_URL` | this server's public address; issuer of its tokens |
| `EXCHANGE_MCP_LINKS_BASE_URL` | address of the link port as sandboxes reach it |
| `EXCHANGE_MCP_AREAS` | the areas published |
| `EXCHANGE_MCP_SIGNIN_LIMITS` | attempts per login and per network per 15 minutes |

Names of hosts, domains and people never appear in this repository; they live in the
installation's configuration.

## Threats

| Threat | Holds it |
|---|---|
| Prompt injection asks to forward, export, add a rule or a delegate | none of these operations exists, at the tool layer or in the EWS allowlist |
| Prompt injection asks to send | a send needs a draft the person sees and `user_confirmed=true`, and draws from the bucket |
| A token stolen from the client | useless without the key; short access lifetime; refresh bound to its client |
| The key stolen | `vault`: not extractable; `file`: Secret readable only by the ServiceAccount, etcd encrypted; tokens rotate with the key |
| Code execution in the pod | sees credentials of active people; no provider prevents it. `adfs` limits it to EWS tokens |
| The sign-in page used to guess or lock accounts | attempt limits per login and network; identical answers |
| SSRF through a person's input | no URL is ever taken from a person: Exchange and AD FS addresses are configuration |
| Reading another person's mailbox | no mailbox argument, no impersonation header; a test inspects every request sent |
