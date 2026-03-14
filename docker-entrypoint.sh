#!/usr/bin/env sh
set -euo pipefail

echo "Running Alembic migrations"
uv run alembic upgrade head

echo "Starting application"
exec "$@"
