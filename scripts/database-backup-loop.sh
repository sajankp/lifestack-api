#!/usr/bin/env bash
set -euo pipefail

run_backup() {
  if /usr/local/bin/database-backup; then
    date -u +%Y-%m-%dT%H:%M:%SZ > /tmp/database-backup-last-success
  else
    echo "Database backup failed" >&2
  fi
}

if [[ "${DB_BACKUP_RUN_ON_START:-false}" == "true" ]]; then
  run_backup
fi

if [[ "${DB_BACKUP_ONCE:-false}" == "true" ]]; then
  [[ -f /tmp/database-backup-last-success ]]
  exit
fi

backup_hour="${DB_BACKUP_HOUR_UTC:-3}"
while true; do
  now_epoch="$(date -u +%s)"
  next_epoch="$(date -u -d "today ${backup_hour}:00" +%s)"
  if (( next_epoch <= now_epoch )); then
    next_epoch="$(date -u -d "tomorrow ${backup_hour}:00" +%s)"
  fi
  sleep_seconds=$((next_epoch - now_epoch))
  echo "Next database backup scheduled in ${sleep_seconds} seconds"
  sleep "${sleep_seconds}"
  run_backup
done
