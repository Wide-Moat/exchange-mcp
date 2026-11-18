# exchange-mcp chart

One Deployment, one Service, an optional Ingress. The server talks to one Exchange installation
over EWS and to nothing in the cluster, so the pod gets no ServiceAccount token.

## How the key reaches the server

```text
deploy job ── scripts/new-sealing-key.sh ──▶ Secret (key; also vault-token or adfs.pem when used)
                 prints the key id only            │ mounted ONLY here
                                                   ▼
                              init container stage-keys (same image)
                                                   │ copies each key, mode 0400, owner = server user
                                                   ▼
                              emptyDir, medium: Memory  ──▶ server container reads the file
```

| Event | What happens |
|---|---|
| container restart (crash, OOM) | the memory volume survives, sessions survive |
| new pod, same Secret | the init container copies the same key, sessions survive |
| deploy with a new key (`key.revision` changes) | pods restart with the new key; everyone signs in again once |
| `kubectl exec … env` | no key: it is never in the environment |
| shell of the server's user reading `/proc/1/…` | refused: the process is not dumpable |
| shell running `cat /run/exchange-mcp/key` | reads it. Grant `pods/exec` in this namespace only to those who may hold the key, or use `key.provider: vault` |

`scripts/chart-e2e.sh` checks each row in a kind cluster on every pull request.

The memory volume is tmpfs: it reaches the node's disk only if the node swaps. Kubernetes nodes run
without swap by default; where swap is on, the staged key can be paged out like any process memory.

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
| `key.existingSecret` | required for `file`, `vault`, `adfs` | the Secret CI writes |
| `key.secretKey` | `key` | key of that Secret holding the sealing key |
| `key.revision` | `""` | the key id `new-sealing-key.sh` printed; a change restarts the pods |
| `key.vault.addr`, `.keyName`, `.mount`, `.tokenSecretKey` | | Vault/OpenBao transit; the token is staged like the key |
| `areas` | all | subset of `mail`, `calendar`, `people`, `contacts`, `tasks` |
| `clientHosts` | any | hosts allowed in registered redirect URIs |
| `replicaCount` | `1` | 1 only: used sign-in codes and attempt counts live in the process, so a second pod would accept a sign-in code twice. Pods are replaced with `Recreate`, so old and new never serve together: a deploy is a few seconds without the server |
| `image.repository` / `.tag` / `.digest` | ghcr image / appVersion / none | a digest pins the image |
| `ingress.enabled`, `.className`, `.annotations`, `.tlsSecretName` | off | host taken from `publicUrl` |
| `resources`, `nodeSelector`, `tolerations`, `affinity`, `podAnnotations`, `podLabels` | | as usual; the selector labels and `exchange-mcp/key-revision` are the chart's and refused here |
