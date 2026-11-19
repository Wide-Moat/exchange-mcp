#!/usr/bin/env bash
# Real stores for the state-store contract suite (tests/unit/test_state_contract.py), one of each shape
# an installation may give the server:
#
#   Valkey standalone with a password                      VALKEY_TEST_URL, VALKEY_TEST_PASSWORD
#   Valkey master behind a Sentinel, no password           VALKEY_TEST_SENTINELS
#   Redis standalone, no password                          REDIS_TEST_URL
#   Redis master behind one Sentinel host, no password     REDIS_TEST_SENTINELS
#
#   eval "$(scripts/stores-test.sh up)"     # starts them and prints the variables
#   scripts/stores-test.sh down
#
# Host networking, so the address a Sentinel announces is the one the tests connect to. Every store runs
# with `maxmemory-policy noeviction`, as the server requires of a real one.
set -euo pipefail
valkey=valkey/valkey:8.1.12-alpine@sha256:9971099af41c36e887aabdfe22b80c0fe45df949163dfc9cb434febc57cfc3fc
redis=redis:7.2.11-alpine@sha256:1a34bdba051ecd8a58ec8a3cc460acef697a1605e918149cc53d920673c1a0a7
names=(ex-state-valkey ex-state-vmaster ex-state-vsentinel ex-state-redis ex-state-rmaster ex-state-rsentinel)
plain=(--save "" --appendonly no --maxmemory-policy noeviction)

sentinel() {  # name image server-binary port master-port
  docker run -d --name "$1" --network host --entrypoint sh "$2" -c \
    "printf 'port $4\nsentinel monitor mymaster 127.0.0.1 $5 1\nsentinel down-after-milliseconds mymaster 2000\n' > /tmp/s.conf && exec $3 /tmp/s.conf" >/dev/null
}
answers() {  # container cli port [args...]
  local c=$1 cli=$2 p=$3
  shift 3
  docker exec "$c" "$cli" -p "$p" "$@" 2>/dev/null
}

case "${1:-}" in
  up)
    password=$(openssl rand -hex 16)
    docker rm -f "${names[@]}" >/dev/null 2>&1 || true
    docker run -d --name ex-state-valkey --network host "$valkey" valkey-server --port 6390 --requirepass "$password" "${plain[@]}" >/dev/null
    docker run -d --name ex-state-vmaster --network host "$valkey" valkey-server --port 6391 "${plain[@]}" >/dev/null
    sentinel ex-state-vsentinel "$valkey" valkey-sentinel 26390 6391
    docker run -d --name ex-state-redis --network host "$redis" redis-server --port 6392 "${plain[@]}" >/dev/null
    docker run -d --name ex-state-rmaster --network host "$redis" redis-server --port 6393 "${plain[@]}" >/dev/null
    sentinel ex-state-rsentinel "$redis" redis-sentinel 26391 6393
    for _ in $(seq 1 30); do
      if answers ex-state-valkey valkey-cli 6390 -a "$password" --no-auth-warning ping | grep -q PONG \
        && answers ex-state-redis redis-cli 6392 ping | grep -q PONG \
        && answers ex-state-vsentinel valkey-cli 26390 sentinel get-master-addr-by-name mymaster | grep -q 6391 \
        && answers ex-state-rsentinel redis-cli 26391 sentinel get-master-addr-by-name mymaster | grep -q 6393; then
        echo "export VALKEY_TEST_URL=redis://127.0.0.1:6390/0"
        echo "export VALKEY_TEST_PASSWORD=$password"
        echo "export VALKEY_TEST_SENTINELS=127.0.0.1:26390"
        echo "export REDIS_TEST_URL=redis://127.0.0.1:6392/0"
        echo "export REDIS_TEST_SENTINELS=127.0.0.1:26391"
        exit 0
      fi
      sleep 1
    done
    echo "the stores did not come up" >&2
    for n in "${names[@]}"; do docker logs "$n" 2>&1 | tail -3 >&2 || true; done
    exit 1
    ;;
  down)
    docker rm -f "${names[@]}" ex-state-master ex-state-sentinel >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: scripts/stores-test.sh up|down" >&2
    exit 2
    ;;
esac
