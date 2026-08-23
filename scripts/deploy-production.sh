#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env.production}"
LOG_DIR="${DEPLOY_LOG_DIR:-${REPO_ROOT}/logs/deploy}"
HEALTH_TIMEOUT_SECONDS="${DEPLOY_HEALTH_TIMEOUT_SECONDS:-180}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-/tmp/lifestack-production-deploy.lock}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

COMPOSE=(
  docker compose
  --env-file "${ENV_FILE}"
  -f "${REPO_ROOT}/docker-compose.yml"
  -f "${REPO_ROOT}/docker-compose.prod.yml"
)
SERVICES=(api migrate cloudflared database-backup)
stack_mutated=false

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

compose_logs() {
  "${COMPOSE[@]}" logs "$@" --timestamps --no-color "${SERVICES[@]}"
}

on_error() {
  local status=$?
  trap - ERR
  echo "Production deployment failed (status ${status})." >&2

  if [[ "${stack_mutated}" == true ]]; then
    local failure_log="${LOG_DIR}/failure-${STAMP}.log"
    compose_logs >"${failure_log}" 2>&1 || true
    echo "Failure logs saved to ${failure_log}" >&2
  fi

  exit "${status}"
}

require_command docker
require_command flock

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Production environment file not found: ${ENV_FILE}" >&2
  exit 1
fi

mkdir -p "${LOG_DIR}"
chmod 700 "${LOG_DIR}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "Another production deployment is already running (lock: ${LOCK_FILE})." >&2
  exit 1
fi

trap on_error ERR

echo "Validating production Compose configuration..."
"${COMPOSE[@]}" config --quiet

predeploy_log="${LOG_DIR}/predeploy-${STAMP}.log"
echo "Saving current container logs to ${predeploy_log}"
compose_logs >"${predeploy_log}" 2>&1 || true

echo "Recreating production services..."
stack_mutated=true
"${COMPOSE[@]}" up -d --build --force-recreate --remove-orphans

poll_seconds=5
attempts=$(( (HEALTH_TIMEOUT_SECONDS + poll_seconds - 1) / poll_seconds ))
api_healthy=false

echo "Waiting up to ${HEALTH_TIMEOUT_SECONDS}s for the API health check..."
for ((attempt = 1; attempt <= attempts; attempt += 1)); do
  api_container="$("${COMPOSE[@]}" ps -q api 2>/dev/null || true)"
  if [[ -n "${api_container}" ]]; then
    health_status="$(docker inspect --format '{{.State.Health.Status}}' "${api_container}" 2>/dev/null || true)"
    if [[ "${health_status}" == healthy ]]; then
      api_healthy=true
      break
    fi
  fi
  sleep "${poll_seconds}"
done

if [[ "${api_healthy}" != true ]]; then
  echo "API did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s." >&2
  "${COMPOSE[@]}" ps >&2 || true
  exit 1
fi

postdeploy_log="${LOG_DIR}/postdeploy-${STAMP}.log"
echo "Production deployment is healthy. Saving startup logs to ${postdeploy_log}"
compose_logs --since 10m | tee "${postdeploy_log}"

echo "Production deployment completed successfully."
