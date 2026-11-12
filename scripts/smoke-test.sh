#!/usr/bin/env bash
# Build the images, start them the way the cluster runs them, and check that
# they work.
#
# Redis, the mock service and the gateway run with the constraints set in k8s/:
# the pods' UIDs, a read-only root filesystem, no capabilities, no privilege
# escalation, Redis with its password and memory cap, and the gateway with the
# config.yaml from its ConfigMap. An image that needs root, or that writes to
# its own filesystem, fails here instead of on the cluster.
#
# CI runs this on every change. It runs the same on a laptop:
#   scripts/smoke-test.sh
# Needs Docker, curl and Python with PyYAML (gateway/requirements.txt has it).
set -euo pipefail
cd "$(dirname "$0")/.."

network=gateway-smoke
password=smoke-test-redis-password
url=http://127.0.0.1:18000      # not 8000, so a running Compose stack is not in the way
work=$(mktemp -d)

cleanup() {
  docker rm -f smoke-gateway smoke-mock smoke-redis >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "FAILED: $1" >&2
  for container in smoke-redis smoke-mock smoke-gateway; do
    echo "--- logs: $container" >&2
    docker logs "$container" >&2 || true
  done
  exit 1
}

# wait_until <what> <command...>: retry the command once a second, for 30 s
wait_until() {
  local what=$1
  shift
  for _ in $(seq 1 30); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "$what"
}

redis_is_ok() {
  [[ "$(curl --silent --fail "$url/admin/health")" == *'"redis":"ok"'* ]]
}

docker build -t gateway:smoke -f gateway/Dockerfile gateway/
docker build -t mock:smoke -f gateway/Dockerfile.mock gateway/

# The images must default to the unprivileged user by themselves: Compose and
# a plain `docker run` have no pod spec to set one
[[ "$(docker run --rm --entrypoint id gateway:smoke -u)" == 10001 ]] || fail "the gateway image does not default to UID 10001"
[[ "$(docker run --rm --entrypoint id mock:smoke -u)" == 10001 ]] || fail "the mock image does not default to UID 10001"

python3 scripts/configmap_file.py k8s/20-gateway-config.yaml gateway-config config.yaml > "$work/config.yaml"

# What every container in k8s/ is held to
pod=(--read-only --cap-drop ALL --security-opt no-new-privileges)

# The network aliases are the Service names the gateway's config points at
docker network create "$network" >/dev/null

# Same arguments as k8s/10-redis.yaml. /data is the one writable path, a volume.
docker run --detach --name smoke-redis --network "$network" --network-alias redis \
  "${pod[@]}" --user 999 --volume /data --env REDISCLI_AUTH="$password" \
  redis:7-alpine redis-server --appendonly yes --loglevel warning \
  --requirepass "$password" --maxmemory 192mb --maxmemory-policy volatile-lru >/dev/null
wait_until "Redis answering the probe command from k8s/10-redis.yaml" \
  docker exec smoke-redis redis-cli -e ping

docker run --detach --name smoke-mock --network "$network" --network-alias mock-service \
  "${pod[@]}" --user 10001:10001 mock:smoke >/dev/null
wait_until "the mock service answering /health" \
  docker exec smoke-mock curl --silent --fail http://localhost:8010/health

docker run --detach --name smoke-gateway --network "$network" --publish 127.0.0.1:18000:8000 \
  "${pod[@]}" --user 10001:10001 \
  --env REDIS_HOST=redis --env REDIS_PORT=6379 --env REDIS_PASSWORD="$password" \
  --env JWT_SECRET_KEY=smoke-test-key-not-used-anywhere-else \
  --volume "$work/config.yaml:/app/config.yaml:ro" gateway:smoke >/dev/null

wait_until "the gateway's liveness endpoint" curl --silent --fail "$url/admin/live"
wait_until "the gateway reaching Redis with its password (/admin/health)" redis_is_ok
wait_until "a request proxied through the gateway to the mock service" curl --silent --fail "$url/mock/users"

echo "smoke test passed"
