#!/usr/bin/env bash
# A new sealing key for exchange-mcp, written by CI into the Secret the chart stages.
#
#   kid=$(scripts/new-sealing-key.sh <namespace> <secret name> [secret key, default "key"])
#   helm upgrade --install … --set key.existingSecret=<secret name> --set key.revision="$kid"
#
# The key never appears in this script's output, its arguments or the arguments of the commands it runs:
# it goes from openssl to a 0600 file in a private directory, and kubectl reads that file. Other keys of
# the same Secret (a Vault token, an AD FS key) are kept. Everyone signs in again after the new key is
# rolled out, once. It prints the key id, which is not secret, for key.revision.
set -euo pipefail
ns=${1:?usage: new-sealing-key.sh <namespace> <secret name> [secret key]}
name=${2:?usage: new-sealing-key.sh <namespace> <secret name> [secret key]}
field=${3:-key}

umask 077
work=$(mktemp -d)
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

kid="k$(date -u +%Y%m%d%H%M%S)$(openssl rand -hex 2)"
material=$(openssl rand 32 | openssl base64 -A | tr '+/' '-_' | tr -d '=')
test "${#material}" -eq 43 || { echo "the generated key is not 32 bytes" >&2; exit 2; }
value=$(printf '%s:%s\n' "$kid" "$material" | openssl base64 -A)
printf '{"data":{"%s":"%s"}}' "$field" "$value" >"$work/patch.json"
unset material value

if kubectl --request-timeout=30s -n "$ns" get secret "$name" >/dev/null 2>&1; then
  kubectl --request-timeout=30s -n "$ns" patch secret "$name" --type merge --patch-file "$work/patch.json" >/dev/null
else
  printf '{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"%s","namespace":"%s"},' "$name" "$ns" >"$work/secret.json"
  tail -c +2 "$work/patch.json" >>"$work/secret.json"
  kubectl --request-timeout=30s create -f "$work/secret.json" >/dev/null
fi
echo "$kid"
