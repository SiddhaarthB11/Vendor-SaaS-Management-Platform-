#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  ENV_FILE="${SCRIPT_DIR}/.env.example"
  echo "infra/.env not found; using infra/.env.example. Create infra/.env before production deployment."
fi

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

usage() {
  cat <<'USAGE'
Usage:
  ./infra/slmct.sh start   Build and start UI, API, and PostgreSQL
  ./infra/slmct.sh stop    Stop and remove running app containers
  ./infra/slmct.sh reset   Stop app, remove DB volume, rebuild, and start from scratch
  ./infra/slmct.sh status  Show container status
  ./infra/slmct.sh logs    Follow service logs
USAGE
}

case "${1:-}" in
  start)
    compose up -d --build
    compose ps
    ;;
  stop)
    compose down --remove-orphans
    ;;
  reset)
    compose down -v --remove-orphans
    compose up -d --build
    compose ps
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f --tail=200
    ;;
  *)
    usage
    exit 1
    ;;
esac
