#!/usr/bin/env sh
set -euo pipefail

echo "Running Alembic migrations"
if command -v uv >/dev/null 2>&1; then
  uv run alembic upgrade head
else
  alembic upgrade head
fi

echo "Starting application"
exec "$@"
