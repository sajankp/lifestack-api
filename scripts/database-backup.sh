#!/usr/bin/env bash
set -euo pipefail

required_vars=(
  DATABASE_URL
  DB_BACKUP_S3_ENDPOINT
  DB_BACKUP_S3_BUCKET
  DB_BACKUP_S3_ACCESS_KEY
  DB_BACKUP_S3_SECRET_KEY
)

for name in "${required_vars[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required backup setting: ${name}" >&2
    exit 1
  fi
done

if [[ -z "${DB_BACKUP_ENCRYPTION_KEY:-}" && "${DB_BACKUP_ALLOW_UNENCRYPTED:-false}" != "true" ]]; then
  echo "DB_BACKUP_ENCRYPTION_KEY is required unless DB_BACKUP_ALLOW_UNENCRYPTED=true" >&2
  exit 1
fi

database_url="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"
region="${DB_BACKUP_S3_REGION:-auto}"
prefix="${DB_BACKUP_PREFIX:-database-backups}"
retention_days="${DB_BACKUP_RETENTION_DAYS:-30}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
work_dir="$(mktemp -d)"
plain_archive="${work_dir}/lifestack-${timestamp}.sql.gz"
upload_file="${plain_archive}"

cleanup() {
  rm -rf "${work_dir}"
}
trap cleanup EXIT

echo "Creating PostgreSQL backup ${timestamp}"
pg_dump --dbname="${database_url}" --format=plain --no-owner --no-privileges | gzip -9 > "${plain_archive}"

if [[ -n "${DB_BACKUP_ENCRYPTION_KEY:-}" ]]; then
  upload_file="${plain_archive}.enc"
  openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \
    -pass env:DB_BACKUP_ENCRYPTION_KEY \
    -in "${plain_archive}" \
    -out "${upload_file}"
fi

(cd "$(dirname "${upload_file}")" && sha256sum "$(basename "${upload_file}")") > "${upload_file}.sha256"
object_key="${prefix%/}/$(basename "${upload_file}")"

export AWS_ACCESS_KEY_ID="${DB_BACKUP_S3_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${DB_BACKUP_S3_SECRET_KEY}"
export AWS_DEFAULT_REGION="${region}"
export AWS_EC2_METADATA_DISABLED=true

aws_args=(--endpoint-url "${DB_BACKUP_S3_ENDPOINT}" --region "${region}")
aws "${aws_args[@]}" s3 cp "${upload_file}" "s3://${DB_BACKUP_S3_BUCKET}/${object_key}" --only-show-errors
aws "${aws_args[@]}" s3 cp "${upload_file}.sha256" \
  "s3://${DB_BACKUP_S3_BUCKET}/${object_key}.sha256" --only-show-errors

echo "Uploaded s3://${DB_BACKUP_S3_BUCKET}/${object_key}"

cutoff_epoch="$(date -u -d "${retention_days} days ago" +%s)"
objects_json="$(aws "${aws_args[@]}" s3api list-objects-v2 \
  --bucket "${DB_BACKUP_S3_BUCKET}" \
  --prefix "${prefix%/}/" \
  --output json)"
objects_file="${work_dir}/objects.json"
printf '%s' "${objects_json}" > "${objects_file}"

python3 - "${cutoff_epoch}" "${DB_BACKUP_S3_BUCKET}" "${DB_BACKUP_S3_ENDPOINT}" "${region}" "${objects_file}" <<'PY'
import datetime
import json
import subprocess
import sys

cutoff = int(sys.argv[1])
bucket, endpoint, region = sys.argv[2:5]
with open(sys.argv[5], encoding="utf-8") as handle:
    payload = json.load(handle)
for item in payload.get("Contents", []):
    modified = item.get("LastModified")
    key = item.get("Key")
    if not modified or not key:
        continue
    epoch = int(datetime.datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp())
    if epoch < cutoff:
        subprocess.run(
            [
                "aws",
                "--endpoint-url",
                endpoint,
                "--region",
                region,
                "s3",
                "rm",
                f"s3://{bucket}/{key}",
                "--only-show-errors",
            ],
            check=True,
        )
PY

echo "Backup completed successfully"
