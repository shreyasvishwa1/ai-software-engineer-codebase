#!/usr/bin/env bash
# Container entrypoint: migrate, then run the API, the worker and Caddy. If any of them stops,
# the container exits so the platform restarts it.
set -euo pipefail

# Hosting platforms provide postgresql:// (or postgres://) URLs; the app requires asyncpg.
case "${DATABASE_URL:-}" in
  postgresql://*) export DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgresql://}" ;;
  postgres://*) export DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgres://}" ;;
esac

mkdir -p "${DATA_DIR:-/data}"
cd /app/backend

alembic upgrade head

# Only Caddy (on this host) may set the client address through X-Forwarded-For.
uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 &
arq app.workers.worker.WorkerSettings &
caddy run --config /app/deploy/Caddyfile --adapter caddyfile &

wait -n
exit 1
