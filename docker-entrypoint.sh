#!/usr/bin/env sh
set -eu

# Migrations are normally run by the dedicated one-shot `migrate` service in
# compose (RUN_MIGRATIONS=false). When the image is run standalone this
# defaults to true so `docker run` still self-migrates.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Running Alembic migrations"
  alembic upgrade head
else
  echo "Skipping Alembic migrations (RUN_MIGRATIONS=${RUN_MIGRATIONS})"
fi

echo "Starting application"
exec "$@"
