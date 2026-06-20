#!/usr/bin/env bash
set -euo pipefail

required_vars=(
  RESTORE_DATABASE_URL
  DB_BACKUP_S3_ENDPOINT
  DB_BACKUP_S3_BUCKET
  DB_BACKUP_S3_ACCESS_KEY
  DB_BACKUP_S3_SECRET_KEY
)

for name in "${required_vars[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required restore setting: ${name}" >&2
    exit 1
  fi
done

if [[ "${CONFIRM_DATABASE_RESTORE:-}" != "restore-lifestack-database" ]]; then
  echo "Set CONFIRM_DATABASE_RESTORE=restore-lifestack-database to continue" >&2
  exit 1
fi

region="${DB_BACKUP_S3_REGION:-auto}"
prefix="${DB_BACKUP_PREFIX:-database-backups}"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

export AWS_ACCESS_KEY_ID="${DB_BACKUP_S3_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${DB_BACKUP_S3_SECRET_KEY}"
export AWS_DEFAULT_REGION="${region}"
export AWS_EC2_METADATA_DISABLED=true
aws_args=(--endpoint-url "${DB_BACKUP_S3_ENDPOINT}" --region "${region}")

object_key="${DB_BACKUP_OBJECT_KEY:-}"
if [[ -z "${object_key}" ]]; then
  object_key="$(aws "${aws_args[@]}" s3api list-objects-v2 \
    --bucket "${DB_BACKUP_S3_BUCKET}" \
    --prefix "${prefix%/}/" \
    --query "reverse(sort_by(Contents[?ends_with(Key, '.sql.gz.enc') || ends_with(Key, '.sql.gz')], &LastModified))[0].Key" \
    --output text)"
fi

if [[ -z "${object_key}" || "${object_key}" == "None" ]]; then
  echo "No database backup object was found" >&2
  exit 1
fi

archive="${work_dir}/$(basename "${object_key}")"
aws "${aws_args[@]}" s3 cp "s3://${DB_BACKUP_S3_BUCKET}/${object_key}" "${archive}" --only-show-errors
aws "${aws_args[@]}" s3 cp "s3://${DB_BACKUP_S3_BUCKET}/${object_key}.sha256" \
  "${archive}.sha256" --only-show-errors

(cd "${work_dir}" && sha256sum --check "$(basename "${archive}").sha256")

compressed_sql="${archive}"
if [[ "${archive}" == *.enc ]]; then
  if [[ -z "${DB_BACKUP_ENCRYPTION_KEY:-}" ]]; then
    echo "DB_BACKUP_ENCRYPTION_KEY is required for encrypted backups" >&2
    exit 1
  fi
  compressed_sql="${archive%.enc}"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -pass env:DB_BACKUP_ENCRYPTION_KEY \
    -in "${archive}" \
    -out "${compressed_sql}"
fi

restore_url="${RESTORE_DATABASE_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"
echo "Restoring ${object_key} into the explicitly selected database"
gzip -dc "${compressed_sql}" | psql "${restore_url}" --set ON_ERROR_STOP=on
echo "Database restore completed"
