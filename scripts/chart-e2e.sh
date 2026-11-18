#!/usr/bin/env bash
# The chart in a real cluster (kind in CI): the key reaches the server the way the chart promises.
#
#   IMAGE=exchange-mcp:e2e scripts/chart-e2e.sh      # the image must already be loaded into the cluster
#
# Each check prints PASS; the script ends by counting them, so a check that silently did not run fails.
# A client_id is sealed with the key, so /authorize accepting it (302) or not (400) shows which key the
# server holds, without signing in to any Exchange.
set -euo pipefail
cd "$(dirname "$0")/.."
image=${IMAGE:?IMAGE=<repository:tag> already loaded into the cluster}
ns=exchange-mcp-e2e
rel=e2e
secret=exchange-mcp-key
host=mcp.example.com
port=18780
passed=0
pass() { echo "PASS $*"; passed=$((passed + 1)); }
fail() { echo "FAIL $*"; exit 1; }


install() {
  helm upgrade --install "$rel" charts/exchange-mcp -n "$ns" --wait --timeout 180s \
    --set image.repository="${image%:*}" --set image.tag="${image##*:}" --set image.pullPolicy=Never \
    --set publicUrl="https://$host" --set exchange.ewsUrl=https://mail.example.com/EWS/Exchange.asmx \
    --set auth.password.loginDomain=corp.example \
    --set key.existingSecret="$secret" --set key.revision="$1" >/dev/null
}
k() { kubectl --request-timeout=30s "$@"; }
server_pod() {
  local p
  p=$(k -n "$ns" get pod -l app.kubernetes.io/instance="$rel" --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}')
  [ -n "$p" ] || fail "no running server pod"
  echo "$p"
}
server() {
  local p
  p=$(server_pod) || exit 1
  timeout 60 kubectl -n "$ns" exec -i "$p" -c server -- "$@"
}

pf_pid=""
forward() {
  [ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null || true
  timeout 600 kubectl -n "$ns" port-forward "svc/$rel-exchange-mcp" "$port:80" >/dev/null 2>&1 &
  pf_pid=$!
  for _ in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && return; sleep 1; done
  fail "the service did not answer through port-forward"
}
trap '[ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null || true' EXIT

register() {
  curl -fsS --max-time 10 -H "Host: $host" -H 'Content-Type: application/json' "http://127.0.0.1:$port/register" \
    -d '{"redirect_uris":["http://127.0.0.1:9/cb"],"token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],"response_types":["code"]}' \
    | jq -er .client_id
}
authorize() {
  curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$port/authorize?client_id=$1&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A9%2Fcb&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&state=s"
}

# 1. CI writes the key; the chart stages it
kubectl create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
kid1=$(scripts/new-sealing-key.sh "$ns" "$secret")
install "$kid1"
pod=$(server_pod)
material=$(k -n "$ns" get secret "$secret" -o jsonpath='{.data.key}' | base64 -d | cut -d: -f2)
[ "${#material}" -eq 43 ] || fail "the Secret does not hold a 32-byte key"

# 2. the server container does not mount the Secret; only the init container does
mounts=$(k -n "$ns" get pod "$pod" -o json)
echo "$mounts" | jq -e '[.spec.containers[] | select(.name=="server") | .volumeMounts[].name] | index("secret") == null' >/dev/null \
  || fail "the server container mounts the Secret"
echo "$mounts" | jq -e '[.spec.initContainers[] | select(.name=="stage-keys") | .volumeMounts[].name] | index("secret") != null' >/dev/null \
  || fail "the init container does not mount the Secret, so this check proves nothing"
pass "the Secret is mounted into the init container only"

# 3. no key in the environment a shell gets
env_out=$(server env)
[ -n "$env_out" ] || fail "env printed nothing: the probe itself is broken"
if grep -qF "$material" <<<"$env_out"; then fail "the key is in the server container's environment"; fi
pass "kubectl exec env shows no key"

# 4. the staged file: the server's user only
mode=$(server stat -c '%a %u' /run/exchange-mcp/key)
[ "$mode" = "400 10001" ] || fail "the staged key is '$mode', not '400 10001'"
pass "the staged key is 0400 and owned by the server's user"

# 5. a shell of the same user cannot read the server process; only an exact refusal passes
got=$(server python - <scripts/proc-probe.py)
[ "$got" = "READ DENIED DENIED" ] || fail "own environ, PID 1 environ, PID 1 maps: $got"
pass "a shell of the server's user cannot read its /proc"

# 6. a container restart keeps the key: what was sealed before still opens
forward
cid=$(register) || fail "client registration failed"
[ "$(authorize "$cid")" = 302 ] || fail "a fresh client id was not accepted"
[ "$(authorize "x${cid:1}")" = 400 ] || fail "a forged client id was accepted: the 302 above proves nothing"
before=$(k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
server sh -c 'kill 1' || true
for _ in $(seq 1 60); do
  now=$(k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
  ready=$(k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].ready}')
  [ "$now" -gt "$before" ] && [ "$ready" = true ] && break
  sleep 2
done
[ "$now" -gt "$before" ] || fail "the server container did not restart"
forward
[ "$(authorize "$cid")" = 302 ] || fail "after a container restart the key changed"
pass "a container restart keeps sessions"

# 7. a new key from CI: pods restart, everything sealed before is refused
kid2=$(scripts/new-sealing-key.sh "$ns" "$secret")
[ "$kid2" != "$kid1" ] || fail "the new key has the old key id"
install "$kid2"
k -n "$ns" rollout status deploy/"$rel-exchange-mcp" --timeout=120s >/dev/null
forward
[ "$(authorize "$cid")" = 400 ] || fail "a client id sealed with the old key still opens"
cid2=$(register) || fail "client registration failed with the new key"
[ "$(authorize "$cid2")" = 302 ] || fail "the server does not work with the new key"
pass "a new key signs everyone out once and works"

want=6
[ "$passed" -eq "$want" ] || fail "$passed of $want checks ran"
echo "chart e2e: $passed of $want checks passed"
