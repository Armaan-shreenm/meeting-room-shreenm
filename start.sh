#!/usr/bin/env bash
#
# Render start command. Runs on every deploy and on every cold start.
#
# preDeployCommand is a paid Render feature, so migrations run here instead.
# The script fails loudly: if `alembic upgrade head` fails, the process exits
# non-zero, Render marks the deploy failed and keeps serving the previous
# version rather than starting a server against a schema it does not match.

set -euo pipefail

echo "==> NM Meet starting (environment=${ENVIRONMENT:-development})"

echo "==> Applying database migrations"
alembic upgrade head
echo "==> Migrations applied"

echo "==> Seeding reference data (rooms, departments, directory)"
python -m scripts.seed
echo "==> Seed complete"

PORT="${PORT:-8000}"
echo "==> Serving on 0.0.0.0:${PORT}"
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips '*'
