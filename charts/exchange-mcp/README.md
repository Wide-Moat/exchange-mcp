# exchange-mcp chart

A Deployment (two replicas by default), a Service, a PodDisruptionBudget, and optional Ingress, HPA and
ServiceMonitor. The server talks to one Exchange installation over EWS and
to the shared state store, and to nothing else in the cluster, so the pod gets no ServiceAccount token.

```text
Ingress (named paths only) ──▶ Service :80 (http) ──▶ pod :8000   /mcp /authorize /token /register /signin
                                                                   /static/ /files/ /adfs/ /.well-known/
Prometheus ───────────────────▶ Service :9090 (metrics) ─▶ pod :9090   /metrics /healthz /readyz
kubelet probes ─────────────────────────────────────────▶ pod :9090   /readyz (key) /healthz (process)
every pod ──▶ Redis/Valkey (state.url, or state.sentinel.hosts ▶ master)   ──▶ Exchange EWS (https)
```

## How the key reaches the server

The key is stable: it lives in a Secret the deployer owns and does not change on deploy, so a rolling
update keeps every session. Create it once, with `scripts/new-sealing-key.sh <namespace> <secret>` (it
prints only the key id), or put one line `<key id>:<base64url of 32 random bytes>` into the deployer's
encrypted secret values, for example from
`printf 'k1:%s\n' "$(openssl rand 32 | basenc --base64url | tr -d '=')"`.

```text
deployer ── Secret (key; also vault-token or adfs.pem when used)    state.existingSecret (passwords)
                                                   │ mounted ONLY here          │
                                                   ▼
                              init container stage-keys (same image) ◀───────────┘
                                                   │ copies each key, mode 0400, owner = server user
                                                   │ (state passwords as state-password, state-sentinelPassword)
                                                   ▼
                              emptyDir, medium: Memory  ──▶ server container reads the file
```

| Event | What happens |
|---|---|
| container restart (crash, OOM) | the memory volume survives, sessions survive |
| new pod, same Secret | the init container copies the same key, sessions survive |
| deploy, same key | rolling update, one pod at a time (`maxSurge: 1`, `maxUnavailable: 0`); nobody signs in again |
| rotation in one step: a new key and a new `key.revision` | pods roll to the new key; everyone signs in again once, and until the rollout ends an old pod refuses what a new pod sealed |
| rotation without sign-outs | the key file's lines: add the new key second, roll out; move it first, roll out; drop the old line later ([auth-modes](../../docs/auth-modes.md#the-sealing-key)) |
| `kubectl exec … env` | no key: it is never in the environment |
| shell of the server's user reading `/proc/1/…` | refused: the process is not dumpable |
| shell running `cat /run/exchange-mcp/key` | reads it. Grant `pods/exec` in this namespace only to those who may hold the key, or use `key.provider: vault` |

`scripts/chart-e2e.sh` checks each row in a kind cluster on every pull request.

The memory volume is tmpfs: it reaches the node's disk only if the node swaps. Kubernetes nodes run
without swap by default; where swap is on, the staged key can be paged out like any process memory.

## The shared state store

Required for more than one replica (the chart refuses `replicaCount > 1` or `autoscaling.enabled`
without it). Redis or Valkey 7 or later, standalone (`state.url`) or behind Sentinel
(`state.sentinel.hosts`; one host, such as a Sentinel Service, is enough). A password is optional.

| Kept | Key | Lifetime |
|---|---|---|
| a redeemed sign-in code | HMAC of the code | 6 minutes |
| sign-in attempts per login and per client network | HMAC of the login / the network | 15 minutes |
| the send allowance per person | HMAC of the person | until refilled, at most a minute |
| an upload running through a link | HMAC of the link | 3 minutes |

Nothing readable: no credential, token, session, address or content, and every key is an HMAC under a
sub-key of the sealing key. Every key has a TTL. Run the store with `maxmemory-policy noeviction`: an
evicted window or code mark would let attempts or a code through, while under `noeviction` a full store
refuses writes and the server fails closed.

| The store does not answer | Effect |
|---|---|
| sign-in | refused, "try again in a minute" (HTTP 503); no password reaches the directory uncounted |
| redeeming a sign-in code | refused, `invalid_request`, the client tries again |
| sending, inviting, cancelling, automatic replies | refused, nothing sent |
| uploads through file links | refused, 503 |
| reading mail, calendar, people, contacts, tasks | goes on |
| `/readyz` | stays 200: readiness follows the key, so a store outage does not empty the Service |
| metrics | `mcp_state_store_up 0`; `mcp_state_store_errors_total{op}` counts |

## Metrics and probes

- `/metrics` on `metrics.port` (9090), never on the public port. Every metric is `mcp_*` with
  `server="exchange-mcp"`; `mcp_build_info{version}` is always 1. `metrics.serviceMonitor.enabled`
  renders a ServiceMonitor for the Prometheus Operator; the Service names the port `metrics`.
- Probes use the metrics port: `/readyz` (the key seals and opens), `/healthz` (the process answers).
- Watch `mcp_state_store_up` (0: sign-in and sending refuse), `mcp_signin_total{result="limited"}`
  (someone guessing passwords) and `mcp_http_requests_total{status_class="5xx"}`.

## Scaling

`autoscaling.enabled` is off. A CPU-utilisation HPA fights a VPA that sets CPU requests: use one of the
two. `autoscaling.inFlightPerPod` adds the Pods metric `mcp_http_requests_in_flight`, which needs
prometheus-adapter; nothing else needs it. No container has a CPU limit; memory has a request and a limit.

## Ingress

Only the paths people and clients use are routed: `/mcp`, `/.well-known/`, `/authorize`, `/token`,
`/register`, `/signin`, `/static/`, `/files/`, `/adfs/`. Never `/`. File links upload up to 25 MB, so
with ingress-nginx set, through `ingress.annotations` (the chart sets none):

| Annotation | Value |
|---|---|
| `nginx.ingress.kubernetes.io/proxy-body-size` | `26m` or more |
| `nginx.ingress.kubernetes.io/proxy-buffering` / `proxy-request-buffering` | `"off"` |
| `nginx.ingress.kubernetes.io/proxy-read-timeout` / `proxy-send-timeout` | `"180"` |

Set `trustedProxies` to the ingress controllers' addresses or CIDRs: the server then reads the client from
`X-Forwarded-For` and limits sign-in attempts per client network too. Unset, that limit is off, because
behind a proxy everybody would share one network and a few failures would lock everyone out. A
server that people reach directly, with no proxy in front, sets `trustedProxies` to an address no
client uses (for example `192.0.2.1`): the per-network limit then counts each connection's own address.

## Values

| Key | Default | Meaning |
|---|---|---|
| `publicUrl` | required | https address people and AI clients reach |
| `exchange.ewsUrl` | required | the EWS endpoint; autodiscover is never used |
| `exchange.caBundle.configMap` / `.key` | none / `ca.crt` | CA bundle for an internal CA |
| `exchange.timezone` | `UTC` | time zone for dates shown to people |
| `auth.mode` | `password` | `password` or `adfs` (adfs not yet run live) |
| `auth.password.loginDomain` | required for `password` | UPN suffix, or the NetBIOS name with `loginFormat: down_level` |
| `auth.password.loginFormat` | `upn` | `upn` or `down_level` |
| `auth.password.loginAlso` | `[]` | more UPN suffixes accepted at sign-in |
| `auth.password.refreshHours` | `12` | how long a session lasts, 1–24 |
| `auth.adfs.issuer`, `.clientId`, `.resource`, `.keyId` | | AD FS settings, see `docs/auth-modes.md` |
| `auth.adfs.signingKeySecretKey` | `adfs.pem` | key of `key.existingSecret` holding the private_key_jwt key |
| `key.provider` | `file` | `file`, `vault` or `memory` |
| `key.existingSecret` | required for `file`, `vault`, `adfs` | the deployer's Secret holding the key |
| `key.secretKey` | `key` | key of that Secret holding the sealing key |
| `key.revision` | `""` | set it to the key id when the key changes; a change restarts the pods |
| `key.vault.addr`, `.keyName`, `.mount`, `.tokenSecretKey` | | Vault/OpenBao transit; the token is staged like the key |
| `areas` | all | subset of `mail`, `calendar`, `people`, `contacts`, `tasks` |
| `clientHosts` | any | hosts allowed in registered redirect URIs |
| `replicaCount` | `2` | more than 1 needs `state`; 1 runs without a store |
| `state.url` | `""` | `redis://host:6379/0` or `rediss://…`, no credentials in it |
| `state.sentinel.hosts` / `.master` | `[]` / `mymaster` | Sentinel `host:port` list (one is enough) and the master group |
| `state.db`, `.tls`, `.username` | `0`, `false`, `""` | database; TLS in Sentinel mode (a URL says it with `rediss://`); an ACL user |
| `state.existingSecret` | `""` | a Secret with `password` and, in Sentinel mode, `sentinelPassword`; empty when the store has no password |
| `autoscaling.*` | off, 2–10, 70% CPU, `inFlightPerPod: null` | see Scaling |
| `podDisruptionBudget.enabled` / `.maxUnavailable` | `true` / `1` | |
| `metrics.port` | `9090` | metrics and probes |
| `metrics.serviceMonitor.enabled` / `.interval` / `.labels` | off / `30s` / `{}` | needs the Prometheus Operator |
| `trustedProxies` | `[]` | proxies whose `X-Forwarded-For` is believed |
| `logLevel` | `INFO` | `LOG_LEVEL`; every line is JSON |
| `tmp.medium` / `.sizeLimit` | `Memory` / `16Mi` | the server's `/tmp`; nothing is spooled there |
| `image.repository` / `.tag` / `.digest` | ghcr image / appVersion / none | a digest pins the image |
| `ingress.enabled`, `.className`, `.annotations`, `.tlsSecretName` | off | host taken from `publicUrl` |
| `resources`, `nodeSelector`, `tolerations`, `affinity`, `podAnnotations`, `podLabels` | | as usual; the selector labels and `exchange-mcp/key-revision` are the chart's and refused here |
