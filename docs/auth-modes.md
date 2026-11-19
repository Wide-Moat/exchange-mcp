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

`password` mode ran against a live Exchange Server 2016 CU23. `adfs` mode has not met a real AD FS
or a real Exchange 2019 CU13+ yet: its unit tests run the whole flow against a fake AD FS, and the
first live run must check the points in
[`live-findings.md`](live-findings.md#not-yet-run-live).

## The login a person types

People type their login three ways: `name`, `DOMAIN\name`, `name@domain`. The operator sets the
one form this Exchange accepts, and the server turns each of the three into it, so the same login
works however it is typed.

| `EXCHANGE_MCP_LOGIN_FORMAT` | `EXCHANGE_MCP_LOGIN_DOMAIN` | Typed | Sent to Exchange |
|---|---|---|---|
| `upn` | `corp.example` | `name`, `CORP\name`, `name@corp.example` | `name@corp.example` |
| `down_level` | `CORP` (with `EXCHANGE_MCP_LOGIN_ALSO=corp.example`) | `name`, `CORP\name`, `name@corp.example` | `CORP\name` |

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
| A refresh token carries the password for a long time | a session lasts 12 hours from sign-in in `password` mode (24 at most) and 30 days in `adfs`; refreshing renews the access token, never the session; rotating the key ends all of them | none |
| A stolen token used elsewhere | tokens are sealed with AES-GCM and bound to kind, person, client, expiry and key id; without the key they are noise | a token stolen together with the key opens the password |
| A token cannot be revoked one by one | nothing is stored, so there is nothing to strike out; the revocation endpoint is off rather than answering a false success | a stolen token works until it expires (access: 1 hour; refresh: 12 hours by default in `password` mode). Changing the password ends that person's tokens; rotating the key ends everyone's |
| A refresh token can be used more than once | it is bound to its client and ends with the session; there is no store in which to detect reuse | a copied refresh token keeps working beside the real one until the session ends |
| Attachment links carry the sealed credential in their URL | the link lives 300 seconds (upload 180), is bound to one person and one attachment, and is useless without the key | a link copied from a sandbox log within those seconds fetches that one attachment |
| TLS to an internal CA | the operator's CA bundle; verification cannot be turned off; `https` only | none |

## Who gets the mailbox

Any MCP client can register itself, so the sign-in page says which application asks and at which
address it receives the answer, before anything else happens. In `adfs` mode that page has its own
button: AD FS may sign the person in without a question (single sign-on), and the person must see the
application first. The browser that pressed the button is the one that must come back from AD FS: a
nonce in a cookie, the same nonce sealed in the AD FS state, compared at the callback. An operator can
also name the only hosts clients may register (`EXCHANGE_MCP_CLIENT_HOSTS`), for example the
organisation's chat.

## The sealing key

Every token, draft reference, cursor and file link is sealed with one key. Where the key lives
sets what a thief has to steal.

| `EXCHANGE_MCP_KEY_PROVIDER` | Where the key is | Pod restart | Replicas | Use it when |
|---|---|---|---|---|
| `vault` | a Vault or OpenBao transit key: it never leaves the secrets manager, and every unseal is in its audit log | nothing happens | any | **recommended** whenever the organisation runs a secrets manager |
| `file` | a Kubernetes Secret the deployer owns, created once (`scripts/new-sealing-key.sh`, or the command in the chart README), mounted only into the chart's init container, which copies it to a memory volume as a file readable by the server's user alone | nothing happens | any | **the required minimum** without a secrets manager |
| `memory` | 32 random bytes born at start, in memory only | everyone signs in again | 1: each pod would hold its own key | a single-replica pilot that accepts a sign-in after every restart |

More than one replica needs the shared state store as well as a shared key: the set of redeemed
sign-in codes, the sign-in attempt windows and the send bucket live there
([architecture](architecture.md#shape)). The chart refuses more than one replica without a store, and
refuses `memory` with more than one.

Rules, enforced at start:

- **Never an environment variable.** It shows in `/proc/<pid>/environ`, in `kubectl exec env` and
  in the Deployment. A key in the environment stops the server.
- **Never typed by a person, never committed, never stored in CI variables in clear.** It is generated
  once into the Secret (`scripts/new-sealing-key.sh` prints only the key id), or kept by the deployer in
  an encrypted secret-values file; no human needs to see it.
- **The server container never mounts the Secret.** The chart's init container is the only one that
  does; it copies each key to a memory volume (`emptyDir`, `medium: Memory`) as a 0400 file owned by the
  server's user. The file outlives a container restart, so a crash keeps everyone signed in; a new pod
  copies the Secret again.
- **The process hides its memory.** On Linux the server marks itself not dumpable before reading any
  setting, so a shell opened with `kubectl exec`, which runs as the same user, cannot read its memory,
  environment or open files through `/proc`.
- **A key file** must be at least 32 bytes and readable only by this server's user and its own group (0400, or 0440 with the pod's fsGroup).
- **The key is stable.** A deploy does not change it, so a rolling update keeps every session: old and
  new pods hold the same key and the same store.
- **Rotation** is a separate operation and a new key id. A key file holds one key per line; the first
  seals, every line opens. Without sign-outs: (1) add the new key as the second line and roll out, so
  every pod opens it; (2) move it to the first line and roll out, so new tokens use it; (3) after the
  longest refresh lifetime, remove the old line. In one step (`scripts/new-sealing-key.sh` and a new
  `key.revision`): tokens sealed under the old key answer 401, clients re-authorise, and each person
  signs in once; during that rollout an old pod also refuses tokens a new pod sealed.

What remains, measured in a kind cluster (`scripts/chart-e2e.sh`):

| Who | Can read the key? | What closes it |
|---|---|---|
| a shell from `kubectl exec` into the server container | yes, `cat` of the staged file, the same as `env` would be | RBAC on `pods/exec` in the namespace; or `vault`, where the pod holds no key |
| the same shell reading the server process (`/proc/1/environ`, `/proc/1/maps`) | no, the process is not dumpable | |
| anyone allowed to read Secrets in the namespace | yes | RBAC on `secrets`, etcd encryption at rest |
| a child process, `ps`, a crash dump, an error reporter | no: the key is never in the environment | |

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

Written out in [`docs/admin-guide.md`](admin-guide.md):
an authentication policy that keeps Modern Auth on EWS only, an AD FS server application with key
authentication and this server's callback, a Web API for Exchange whose `scp` rule carries
`EWS.AccessAsUser.All` and `offline_access` and nothing for ActiveSync, Exchange's trust in AD FS
(`New-AuthServer -Type ADFS`), and turning off automatic forwarding to external domains.
