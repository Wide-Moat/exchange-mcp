# Architecture

## Shape

```
MCP client (gateway, chat UI, desktop client)
   │  Streamable HTTP /mcp, OAuth 2.1 (MCP authorization: metadata, dynamic registration, PKCE)
   ▼
exchange-mcp :8000 ── /mcp  /authorize  /token  /register  /signin  /static/*  /adfs/callback  /.well-known/*
                     /files/d/<link>  /files/u/<link>   (sealed attachment and body links)
                     /healthz  /readyz                  (also on :9090; the Ingress routes neither)
exchange-mcp :9090 ── /metrics  /metrics/  /healthz  /readyz   (never routed by the Ingress; probes use it)
   │
   ├─ oauth/      the authorization server; every artefact is a sealed blob
   ├─ state/      the shared state store: used codes, sign-in windows, send bucket, upload marks
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

Many replicas
   │  same sealing key (a Secret the deployer owns), same state store
   ▼
Redis or Valkey ── standalone (EXCHANGE_MCP_STATE_URL) or behind Sentinel (EXCHANGE_MCP_STATE_SENTINELS)
```

No session store: tokens are sealed, so any replica holding the key serves any person, and a restart
keeps everyone signed in. What every replica must agree on lives in the shared state store:

| Kept in the store | Key (never a value in clear) | Lifetime | Without it, with N replicas |
|---|---|---|---|
| a redeemed authorization code | HMAC of the code | the code's life + 1 min | the code redeemed N times |
| sign-in attempts per login, per client network | HMAC of the login / network | 15 min sliding window | N times the attempts before a directory lockout |
| the send bucket per person | HMAC of the person key | until refilled | N times the allowance |
| an upload running through a link | HMAC of the link's id | 3 min | two uploads through one link at once |

The HMAC key is derived from the sealing key and used for nothing else. The store holds no credential,
token, session, address or content. Without a store configured the state lives in the process, which is
correct for exactly one replica; the chart refuses more. When the store does not answer, sign-in, code
redemption, sending and uploads refuse with "try again shortly" (fail closed); reading goes on, and the
pods stay ready (`/readyz` follows the key, not the store) while `mcp_state_store_up` drops to 0.

```text


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
| `oauth/` | metadata, registration (the `client_id` is a sealed registration), codes (5 min, used once across all replicas through `state/`), access (1 h), refresh (`adfs` 30 days, `password` 12 h) | a database |
| `state/` | one interface (`once`, `release`, `take_window`, `take_token`), in process or in Redis/Valkey; keys are HMACs; failures raise `StoreUnavailable` | a credential, a token or content |
| `core/guard.py` | `user_confirmed is True`, the per-person bucket, recipient caps | lets a tool reach EWS before both pass |
| `core/html.py` | outgoing pipeline (sanitise → `cid:` → inline CSS → validate) and the reader (HTML → text, hidden text dropped) | n/a |
| `links/` | HMAC-signed, expiring, person-bound links; streaming in both directions | holds a file whole in memory |
| `observability.py` | one JSON object per line from every process (`ts`, `level`, `logger`, `event`, `msg`, allowlisted fields, `exc` with frames only); `LOG_LEVEL`; a filter that drops any record with an address, a JWT or a token shape | content |
| `metrics.py` | the shared `mcp_*` Prometheus metrics with `server="exchange-mcp"`, served on the metrics port only | a person, login or argument in a label |

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
| `EXCHANGE_MCP_PUBLIC_URL` | this server's public address; issuer of its tokens and the base of file links |
| `EXCHANGE_MCP_AREAS` | the areas published |
| `EXCHANGE_MCP_CLIENT_HOSTS` | optional: the only hosts MCP clients may register redirect addresses on (the organisation's chat or gateway); empty allows any https or loopback address |
| `EXCHANGE_MCP_ATTEMPTS_PER_LOGIN`, `EXCHANGE_MCP_ATTEMPTS_PER_NETWORK` | sign-in attempts per 15 minutes (5 and 20) |
| `EXCHANGE_MCP_TRUSTED_PROXIES` | addresses or CIDRs of the proxies whose `X-Forwarded-For` names the client; unset turns the per-network limit off |
| `EXCHANGE_MCP_STATE_URL` | `redis://host:6379/0` or `rediss://…`: the shared state store, standalone |
| `EXCHANGE_MCP_STATE_SENTINELS`, `_STATE_SENTINEL_MASTER` | `host:26379,…` and the master group (`mymaster`): the store behind Sentinel; one host is enough |
| `EXCHANGE_MCP_STATE_DB`, `_STATE_TLS`, `_STATE_USERNAME`, `_STATE_PREFIX` | database (0), TLS for Sentinel mode, an ACL user, key prefix (`exchange-mcp:`) |
| `EXCHANGE_MCP_STATE_PASSWORD_FILE`, `_STATE_SENTINEL_PASSWORD_FILE` | optional: passwords, from files only (0400, owned by the server's user) |
| `EXCHANGE_MCP_METRICS_PORT` | the metrics and probe port (9090), never the public one |
| `LOG_LEVEL` | `INFO` by default; unprefixed |

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
| The sign-in page used to guess or lock accounts | attempt limits per login and network, shared by every replica through the store; identical answers; a store that does not answer refuses sign-in rather than count nothing |
| The state store read or written by someone else | it holds HMAC-named counters and marks, no credential or content; a forged "used" mark refuses a code, a deleted window resets a limit; give the store a password or a NetworkPolicy of its own when that matters |
| SSRF through a person's input | no URL is ever taken from a person: Exchange and AD FS addresses are configuration |
| Reading another person's mailbox | no mailbox argument, no impersonation header; a test inspects every request sent |
