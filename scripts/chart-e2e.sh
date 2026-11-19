#!/usr/bin/env bash
# The chart in a real cluster (kind in CI): three replicas, Valkey behind Sentinel without a password,
# a stand-in Exchange that checks NTLM passwords over TLS, and the key delivered the way the chart
# promises.
#
#   IMAGE=exchange-mcp:e2e scripts/chart-e2e.sh                 # every check must pass
#   IMAGE=<an older image> KNOWN_BAD=1 scripts/chart-e2e.sh     # every replica check must FAIL
#
# The image must already be loaded into the cluster, and metrics-server installed (the HPA check).
# Each check prints PASS (or, with KNOWN_BAD, FAILS AS IT SHOULD); the script ends by counting them,
# so a check that silently did not run fails the run. KNOWN_BAD runs the replica checks against an
# image without the shared store (0.1.x): a check that passes there proves nothing, and fails the run.
#
# Pods are reached one by one through port-forwards to each pod, never through the Service, so "on pod
# A, then on pod B" means two different processes. Sign-in goes to the stand-in Exchange
# (tests/e2e/fake_ews.py); a client_id is sealed with the key, so /authorize accepting it (302) or not
# (400) shows which key a server holds.
set -euo pipefail
cd "$(dirname "$0")/.."
image=${IMAGE:?IMAGE=<repository:tag> already loaded into the cluster}
known_bad=${KNOWN_BAD:-}
# A namespace of this run's own, removed when it ends: a store promoted by an earlier run, or a stand-in
# Exchange still serving an earlier certificate, cannot leak into it, and no other run's is touched.
ns="exchange-mcp-e2e-$(openssl rand -hex 3)"
rel=e2e
secret=exchange-mcp-key
host=mcp.example.com
port=18780
replicas=3
passed=0
expected_failures=0
pass() { echo "PASS $*"; passed=$((passed + 1)); }
fail() {
  echo "FAIL $*"
  k -n "$ns" get pods -o wide 2>/dev/null || true
  k -n "$ns" logs -l app.kubernetes.io/instance=e2e --all-containers --tail=40 --prefix 2>/dev/null || true
  exit 1
}
k() { kubectl --request-timeout=30s "$@"; }
work=$(mktemp -d)
pids=()
cleanup() {
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  rm -rf "$work"
  if [ -z "${KEEP:-}" ]; then k delete namespace "$ns" --wait=false >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

# A replica check: a function that returns 0 when the property holds and sets $why when it does not.
# With KNOWN_BAD the check must fail, and for the reason named (a regular expression): a failure for any
# other reason (a port-forward that broke, a sign-in that did not work) proves nothing about the property.
why=""
replica_check() {
  local name=$1 fn=$2 expected=$3
  why=""
  if "$fn"; then
    if [ -n "$known_bad" ]; then fail "$name passes on an image without the shared store: it proves nothing"; fi
    pass "$name"
  else
    if [ -z "$known_bad" ]; then fail "$name: $why"; fi
    grep -Eq "$expected" <<<"$why" || fail "$name failed for another reason than the one it tests: $why"
    echo "FAILS AS IT SHOULD $name: $why"
    expected_failures=$((expected_failures + 1))
  fi
}

# ---------------------------------------------------------------- the cluster around the server
k create namespace "$ns" >/dev/null
echo "namespace $ns"
sed "s/__NS__/$ns/g" tests/e2e/valkey-sentinel.yaml | k -n "$ns" apply -f - >/dev/null
sentinels="valkey-sentinel-0.valkey-sentinel.$ns.svc.cluster.local:26379"
sentinels="$sentinels,valkey-sentinel-1.valkey-sentinel.$ns.svc.cluster.local:26379"
sentinels="$sentinels,valkey-sentinel-2.valkey-sentinel.$ns.svc.cluster.local:26379"

# The stand-in Exchange: a CA made for this run, a certificate for its Service name, one user.
ews_host="fake-ews.$ns.svc"
(
  cd "$work"
  openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -subj /CN=e2e-ca -days 1 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout tls.key -out tls.csr -subj "/CN=$ews_host" 2>/dev/null
  printf 'subjectAltName=DNS:%s\n' "$ews_host" >ext
  openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out tls.crt -days 1 -extfile ext 2>/dev/null
)
password="e2e-$(openssl rand -hex 8)"
printf ':alex@corp.example:%s\n' "$password" >"$work/users"
k -n "$ns" create secret tls fake-ews-tls --cert="$work/tls.crt" --key="$work/tls.key" --dry-run=client -o yaml | k apply -f - >/dev/null
k -n "$ns" create configmap fake-ews --from-file=fake_ews.py=tests/e2e/fake_ews.py --from-file=users="$work/users" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
k -n "$ns" create configmap ews-ca --from-file=ca.crt="$work/ca.crt" --dry-run=client -o yaml | k apply -f - >/dev/null
cat <<EOF | k -n "$ns" apply -f - >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata: {name: fake-ews}
spec:
  replicas: 1
  selector: {matchLabels: {app: fake-ews}}
  template:
    metadata: {labels: {app: fake-ews}}
    spec:
      securityContext: {runAsUser: 10001, runAsNonRoot: true}
      containers:
        - name: ews
          image: $image
          imagePullPolicy: Never
          command: [python, /fake/fake_ews.py, /tls/tls.crt, /tls/tls.key]
          env: [{name: NTLM_USER_FILE, value: /fake/users}]
          ports: [{containerPort: 8443}]
          readinessProbe: {tcpSocket: {port: 8443}, periodSeconds: 2}
          volumeMounts:
            - {name: fake, mountPath: /fake}
            - {name: tls, mountPath: /tls}
      volumes:
        - {name: fake, configMap: {name: fake-ews}}
        - {name: tls, secret: {secretName: fake-ews-tls, defaultMode: 0444}}
---
apiVersion: v1
kind: Service
metadata: {name: fake-ews}
spec:
  selector: {app: fake-ews}
  ports: [{port: 8443, targetPort: 8443}]
EOF
k -n "$ns" rollout status statefulset/valkey --timeout=180s >/dev/null
k -n "$ns" rollout status statefulset/valkey-sentinel --timeout=180s >/dev/null
k -n "$ns" rollout status deploy/fake-ews --timeout=180s >/dev/null
master_of() {
  k -n "$ns" exec valkey-sentinel-0 -- valkey-cli -p 26379 sentinel get-master-addr-by-name mymaster | head -1
}
[ "$(master_of)" = "$(k -n "$ns" get pod valkey-0 -o jsonpath='{.status.podIP}')" ] || fail "Sentinel does not name valkey-0 as master"

install() {
  local wait=(--wait --timeout 240s)
  [ -n "$known_bad" ] && wait=()  # its pods never turn ready: the old image has no /readyz on 9090
  timeout 300 helm upgrade --install "$rel" charts/exchange-mcp -n "$ns" "${wait[@]}" \
    --set image.repository="${image%:*}" --set image.tag="${image##*:}" --set image.pullPolicy=Never \
    --set publicUrl="https://$host" --set exchange.ewsUrl="https://$ews_host:8443/EWS/Exchange.asmx" \
    --set exchange.caBundle.configMap=ews-ca --set auth.password.loginDomain=corp.example \
    --set replicaCount="$replicas" --set "state.sentinel.hosts={$sentinels}" \
    --set key.existingSecret="$secret" --set key.revision="$1" "${@:2}" >/dev/null
}
server_pods() {
  k -n "$ns" get pod -l app.kubernetes.io/instance="$rel" -o json \
    | jq -r '.items[] | select(.status.phase == "Running" and .metadata.deletionTimestamp == null) | .metadata.name'
}
server_pod() {
  local p
  p=$(server_pods | head -1)
  [ -n "$p" ] || fail "no running server pod"
  echo "$p"
}
server() {
  local p
  p=$(server_pod) || exit 1
  timeout 60 kubectl -n "$ns" exec -i "$p" -c server -- "$@"
}

svc_pid=""
forward() {
  [ -n "$svc_pid" ] && kill "$svc_pid" 2>/dev/null || true
  timeout 900 kubectl -n "$ns" port-forward "svc/$rel-exchange-mcp" "$port:80" >/dev/null 2>&1 &
  svc_pid=$!
  pids+=("$svc_pid")
  for _ in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && return; sleep 1; done
  fail "the service did not answer through port-forward"
}

# One port-forward per pod: main port at 18800+i, metrics port at 18900+i.
pods=()
forward_pods() {
  local i=0 p
  mapfile -t pods < <(server_pods | head -"$replicas")
  [ "${#pods[@]}" -eq "$replicas" ] || fail "${#pods[@]} running server pods, not $replicas"
  # one port-forward per port: kubectl ends a whole port-forward when one forwarded connection fails, and
  # an image without a metrics port (the known-bad run) must not take the main port's forward with it
  for p in "${pods[@]}"; do
    timeout 900 kubectl -n "$ns" port-forward "pod/$p" "$((18800 + i)):8000" >/dev/null 2>&1 &
    pids+=("$!")
    timeout 900 kubectl -n "$ns" port-forward "pod/$p" "$((18900 + i)):9090" >/dev/null 2>&1 &
    pids+=("$!")
    i=$((i + 1))
  done
  for i in $(seq 0 $((replicas - 1))); do
    local attempt answered=""
    for attempt in $(seq 1 30); do
      if [ "$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$((18800 + i))/healthz")" = 200 ]; then
        answered=$attempt
        break
      fi
      sleep 1
    done
    [ -n "$answered" ] || fail "pod ${pods[$i]} does not answer through its port-forward"
  done
  # the stand-in Exchange signs alex in on these pods: the replica checks below depend on it
  python3 tests/e2e/flow.py code "$(main_port 0)" alex "$password" >/dev/null || fail "the baseline sign-in on ${pods[0]} failed"
}
main_port() { echo $((18800 + $1)); }
metrics_port() { echo $((18900 + $1)); }
readyz() { curl -s --max-time 5 -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$(main_port "$1")/readyz"; }

register() {
  curl -fsS --max-time 10 -H "Host: $host" -H 'Content-Type: application/json' "http://127.0.0.1:$port/register" \
    -d '{"redirect_uris":["http://127.0.0.1:9/cb"],"token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],"response_types":["code"]}' \
    | jq -er .client_id
}
authorize() {
  curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$port/authorize?client_id=$1&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A9%2Fcb&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&state=s"
}

# ---------------------------------------------------------------- the replica checks (spec section 5)

# 1. a code redeemed on pod A is refused on pod B. First, B redeems a fresh code A issued: B opens what A
# sealed, so the refusal that follows is about the code being used, not about B being unable to read it.
check_code_once() {
  local out cid verifier code across first second
  out=$(python3 tests/e2e/flow.py code "$(main_port 0)" alex "$password") || { why="sign-in on pod A failed"; return 1; }
  read -r cid verifier code <<<"$out"
  across=$(python3 tests/e2e/flow.py redeem "$(main_port 1)" "$cid" "$verifier" "$code")
  out=$(python3 tests/e2e/flow.py code "$(main_port 0)" alex "$password") || { why="sign-in on pod A failed"; return 1; }
  read -r cid verifier code <<<"$out"
  first=$(python3 tests/e2e/flow.py redeem "$(main_port 0)" "$cid" "$verifier" "$code")
  second=$(python3 tests/e2e/flow.py redeem "$(main_port 1)" "$cid" "$verifier" "$code")
  echo "  a code from ${pods[0]} redeemed on ${pods[1]}: $across; another redeemed on ${pods[0]}: $first, then on ${pods[1]}: $second"
  [ "$across" = "200" ] || { why="pod B could not redeem a fresh code from pod A: $across"; return 1; }
  [ "$first" = "200" ] || { why="the first redemption answered $first"; return 1; }
  [ "$second" = "400 invalid_grant" ] || { why="pod B answered $second to a code pod A had redeemed"; return 1; }
}

# 2. the sign-in attempt limit (5 per login) is shared: five wrong passwords spread over three pods,
# then the sixth is limited on whichever pod it reaches
check_limit_shared() {
  local i got codes=""
  for i in 0 1 2 3 4; do
    got=$(python3 tests/e2e/flow.py attempt "$(main_port $((i % replicas)))" bob wrong-password)
    codes="$codes $got"
    [ "$got" = 400 ] || { why="attempt $((i + 1)) answered $got, not 400"; return 1; }
  done
  got=$(python3 tests/e2e/flow.py attempt "$(main_port 2)" bob wrong-password)
  echo "  attempts on pods 0,1,2,0,1:$codes; the sixth on pod 2: $got"
  [ "$got" = 429 ] || { why="the sixth attempt answered $got: each pod counted on its own"; return 1; }
}

# 5. the master stops answering: on every pod the store is reported down (mcp_state_store_up 0) while
# readiness stays 200 (a store outage must not empty the Service), and sign-in is refused (fail closed).
# Sentinel promotes the replica, and sign-in works again within 30 seconds of the freeze.
store_up() {
  curl -fsS --max-time 5 "http://127.0.0.1:$(metrics_port "$1")/metrics" 2>/dev/null \
    | sed -n 's/^mcp_state_store_up{server="exchange-mcp"} //p'
}
check_failover() {
  local before t0 i deadline all_down=0 ready="" during after out elapsed
  before=$(master_of)
  timeout 60 kubectl -n "$ns" exec valkey-0 -- valkey-cli DEBUG SLEEP 45 >/dev/null 2>&1 &
  pids+=("$!")
  t0=$(date +%s)
  deadline=$((t0 + 9))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    all_down=1
    ready=""
    for i in $(seq 0 $((replicas - 1))); do
      ready="$ready $(readyz "$i")"  # readiness pings the store, which also refreshes the gauge
      [ "$(store_up "$i")" = "0.0" ] || all_down=0
    done
    [ "$all_down" = 1 ] && break
    sleep 0.5
  done
  during=$(python3 tests/e2e/flow.py attempt "$(main_port 0)" alex "$password")
  echo "  master frozen: store down on every pod: $([ "$all_down" = 1 ] && echo yes || echo no); readyz:$ready; sign-in on pod A: $during"
  [ "$all_down" = 1 ] || { why="mcp_state_store_up did not turn 0 on every pod while the master was frozen"; return 1; }
  [ "$ready" = "$(printf ' 200%.0s' $(seq 1 "$replicas"))" ] || { why="readiness followed the store:$ready"; return 1; }
  [ "$during" = 503 ] || { why="sign-in answered $during while the store was gone, not 503 (fail closed)"; return 1; }
  deadline=$((t0 + 30))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    after=$(master_of 2>/dev/null || true)
    if [ -n "$after" ] && [ "$after" != "$before" ]; then
      if out=$(timeout $((deadline - $(date +%s) + 1)) python3 tests/e2e/flow.py code "$(main_port 1)" alex "$password" 2>/dev/null) \
        && [ -n "$out" ]; then
        elapsed=$(($(date +%s) - t0))
        echo "  master $before -> $after; a sign-in on pod B issued a code ${elapsed}s after the freeze"
        [ "$elapsed" -le 30 ] || { why="sign-in came back after ${elapsed}s, not within 30"; return 1; }
        return 0
      fi
    fi
    sleep 1
  done
  why="no working sign-in within 30 s of the master freezing (sentinel names: ${after:-nothing})"
  return 1
}

# 6. every line of every container of every server pod, the init container included, is JSON
check_logs_json() {
  local p c total=0 n
  for p in "${pods[@]}"; do
    for c in stage-keys server; do
      k -n "$ns" logs "$p" -c "$c" >"$work/log" 2>/dev/null || { why="no logs for $p/$c"; return 1; }
      if ! n=$(python3 -c '
import json, sys
lines = [l for l in open(sys.argv[1]) if l.strip()]
for l in lines:
    r = json.loads(l)
    assert isinstance(r, dict) and {"ts", "level", "logger", "event", "msg"} <= set(r), l
print(len(lines))' "$work/log" 2>/dev/null); then
        why="$p/$c wrote a line that is not a JSON record: $(grep -v '^{' "$work/log" | head -1 | cut -c1-120)"
        return 1
      fi
      [ "$n" -gt 0 ] || { why="$p/$c wrote nothing"; return 1; }
      total=$((total + n))
    done
  done
  echo "  $total JSON lines from $((${#pods[@]} * 2)) containers"
}

# 7. /metrics on port 9090 of every pod, with the shared names; none on the main port
check_metrics() {
  local i body main
  for i in $(seq 0 $((replicas - 1))); do
    body=$(curl -fsS --max-time 5 "http://127.0.0.1:$(metrics_port "$i")/metrics" 2>/dev/null) \
      || { why="${pods[$i]} serves no /metrics on 9090"; return 1; }
    grep -q '^mcp_http_requests_in_flight{' <<<"$body" || { why="${pods[$i]}: no mcp_http_requests_in_flight"; return 1; }
    grep -Eq '^mcp_state_store_up\{server="exchange-mcp"\} 1(\.0)?$' <<<"$body" \
      || { why="${pods[$i]}: mcp_state_store_up is not 1"; return 1; }
    grep -q '^mcp_build_info{server="exchange-mcp",version=' <<<"$body" || { why="${pods[$i]}: no mcp_build_info"; return 1; }
    [ "$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$(metrics_port "$i")/readyz")" = 200 ] \
      || { why="${pods[$i]}: no /readyz on the metrics port"; return 1; }
    main=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$(main_port "$i")/metrics")
    [ "$main" = 404 ] || { why="${pods[$i]} answers $main for /metrics on the main port"; return 1; }
  done
  echo "  $replicas pods: /metrics and /readyz on 9090, build info, in flight, store up 1; main port /metrics 404"
}

replica_checks() {
  forward_pods
  replica_check "every pod serves metrics and probes on 9090, and no metrics on the main port" check_metrics \
    "serves no /metrics on 9090"
  replica_check "a code redeemed on pod A is refused on pod B" check_code_once "pod B answered 200 to a code pod A"
  replica_check "sign-in attempts are limited across pods at the configured total" check_limit_shared \
    "the sixth attempt answered 400"
  replica_check "the store's master freezes: store down, pods stay ready, sign-in fails closed, then works after failover within 30 s" \
    check_failover "mcp_state_store_up did not turn 0"
  replica_check "every log line of every container, the init container included, is JSON" check_logs_json \
    "stage-keys wrote a line that is not a JSON record"
  echo "NOTE the send bucket is shared across replicas: proven against real Valkey in"
  echo "     tests/unit/test_replicas.py::test_the_send_bucket_is_shared_across_replicas (it needs Exchange to send)"
}

kid1=$(scripts/new-sealing-key.sh "$ns" "$secret")
install "$kid1"

if [ -n "$known_bad" ]; then
  # The old image has no metrics port, so this chart's probes (on 9090) would restart its containers
  # every 90 seconds and break the port-forwards mid-check. They are removed for this run only: what is
  # measured is the old server's behaviour, not its probes.
  k -n "$ns" patch deploy "$rel-exchange-mcp" --type json -p '[
    {"op": "remove", "path": "/spec/template/spec/containers/0/livenessProbe"},
    {"op": "remove", "path": "/spec/template/spec/containers/0/readinessProbe"}]' >/dev/null
  k -n "$ns" rollout status deploy/"$rel-exchange-mcp" --timeout=240s >/dev/null
  for _ in $(seq 1 90); do
    [ "$(server_pods | wc -l)" -ge "$replicas" ] && break
    sleep 2
  done
  replica_checks
  want=5
  [ "$expected_failures" -eq "$want" ] || fail "$expected_failures of $want replica checks failed on the old image"
  echo "chart e2e against a known-bad image: all $want replica checks fail, as they must"
  exit 0
fi

# ---------------------------------------------------------------- the key (0.1.7 checks)
pod=$(server_pod)
material=$(k -n "$ns" get secret "$secret" -o jsonpath='{.data.key}' | base64 -d | cut -d: -f2)
[ "${#material}" -eq 43 ] || fail "the Secret does not hold a 32-byte key"

mounts=$(k -n "$ns" get pod "$pod" -o json)
echo "$mounts" | jq -e '[.spec.containers[] | select(.name=="server") | .volumeMounts[].name] | index("secret") == null' >/dev/null \
  || fail "the server container mounts the Secret"
echo "$mounts" | jq -e '[.spec.initContainers[] | select(.name=="stage-keys") | .volumeMounts[].name] | index("secret") != null' >/dev/null \
  || fail "the init container does not mount the Secret, so this check proves nothing"
pass "the Secret is mounted into the init container only"

env_out=$(server env)
[ -n "$env_out" ] || fail "env printed nothing: the probe itself is broken"
if grep -qF "$material" <<<"$env_out"; then fail "the key is in the server container's environment"; fi
pass "kubectl exec env shows no key"

mode=$(server stat -c '%a %u' /run/exchange-mcp/key)
[ "$mode" = "400 10001" ] || fail "the staged key is '$mode', not '400 10001'"
pass "the staged key is 0400 and owned by the server's user"

got=$(server python - <scripts/proc-probe.py)
[ "$got" = "READ DENIED DENIED" ] || fail "own environ, PID 1 environ, PID 1 maps: $got"
pass "a shell of the server's user cannot read its /proc"

# ---------------------------------------------------------------- many replicas
replica_checks

# ---------------------------------------------------------------- the key across restarts
# Everything through a port-forward to the pod whose container restarts, never the Service: another
# replica answering would hide a key lost in the restart.
svc_port=$port
port=18799
forward_pod() {
  [ -n "$svc_pid" ] && kill "$svc_pid" 2>/dev/null || true
  timeout 900 kubectl -n "$ns" port-forward "pod/$1" "$port:8000" >/dev/null 2>&1 &
  svc_pid=$!
  pids+=("$svc_pid")
  for _ in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && return; sleep 1; done
  fail "pod $1 did not answer through port-forward"
}
forward_pod "$pod"
cid=$(register) || fail "client registration failed"
[ "$(authorize "$cid")" = 302 ] || fail "a fresh client id was not accepted"
[ "$(authorize "x${cid:1}")" = 400 ] || fail "a forged client id was accepted: the 302 above proves nothing"
before=$(k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
timeout 60 kubectl -n "$ns" exec "$pod" -c server -- sh -c 'kill 1' || true
for _ in $(seq 1 60); do
  now=$(k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
  ready=$(k -n "$ns" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].ready}')
  [ "$now" -gt "$before" ] && [ "$ready" = true ] && break
  sleep 2
done
[ "$now" -gt "$before" ] || fail "the server container did not restart"
forward_pod "$pod"
[ "$(authorize "$cid")" = 302 ] || fail "after a container restart the key changed"
pass "a container restart keeps sessions"
port=$svc_port

kid2=$(scripts/new-sealing-key.sh "$ns" "$secret")
[ "$kid2" != "$kid1" ] || fail "the new key has the old key id"
install "$kid2"
k -n "$ns" rollout status deploy/"$rel-exchange-mcp" --timeout=180s >/dev/null
forward
[ "$(authorize "$cid")" = 400 ] || fail "a client id sealed with the old key still opens"
cid2=$(register) || fail "client registration failed with the new key"
[ "$(authorize "$cid2")" = 302 ] || fail "the server does not work with the new key"
pass "a new key signs everyone out once and works"

# ---------------------------------------------------------------- 8. the HPA, CPU at least
k -n kube-system rollout status deploy/metrics-server --timeout=120s >/dev/null || fail "metrics-server is not running"
install "$kid2" --set autoscaling.enabled=true --set autoscaling.minReplicas="$replicas" --set autoscaling.maxReplicas=5
cpu=""
for _ in $(seq 1 60); do
  cpu=$(k -n "$ns" get hpa "$rel-exchange-mcp" -o jsonpath='{.status.currentMetrics[?(@.type=="Resource")].resource.current.averageUtilization}' 2>/dev/null || true)
  [ -n "$cpu" ] && break
  sleep 5
done
k -n "$ns" get hpa
[ -n "$cpu" ] || fail "the HPA shows no CPU measurement after 5 minutes"
pass "the HPA exists and measures CPU (${cpu}% of the request)"

want=12
[ "$passed" -eq "$want" ] || fail "$passed of $want checks ran"
echo "chart e2e: $passed of $want checks passed"
