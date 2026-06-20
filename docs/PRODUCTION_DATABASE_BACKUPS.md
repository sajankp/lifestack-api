# Production Database Backups

The production compose stack includes a dedicated `database-backup` sidecar. It creates a compressed
plain-SQL PostgreSQL dump, encrypts it client-side, uploads the encrypted archive and SHA-256 checksum
to S3-compatible object storage, and removes objects older than the configured retention window.

The API and Cloudflare Tunnel do not perform backup work. This separation keeps `pg_dump`, object
storage credentials, and encryption material outside the public API process.

## Supported Providers

### Cloudflare R2

Create a private R2 bucket and a scoped R2 API token with Object Read & Write access for that bucket.
Use the S3 endpoint shown in the R2 dashboard:

```dotenv
DB_BACKUP_S3_ENDPOINT=https://<cloudflare-account-id>.r2.cloudflarestorage.com
DB_BACKUP_S3_BUCKET=lifestack-backups
DB_BACKUP_S3_REGION=auto
DB_BACKUP_S3_ACCESS_KEY=<r2-access-key-id>
DB_BACKUP_S3_SECRET_KEY=<r2-secret-access-key>
```

Reference: <https://developers.cloudflare.com/r2/api/s3/tokens/>

### Oracle Cloud Object Storage

Create an Object Storage bucket, generate a Customer Secret Key for the deployment user, and use the
namespace-specific S3 Compatibility endpoint:

```dotenv
DB_BACKUP_S3_ENDPOINT=https://<namespace>.compat.objectstorage.<region>.oraclecloud.com
DB_BACKUP_S3_BUCKET=lifestack-backups
DB_BACKUP_S3_REGION=<oci-region>
DB_BACKUP_S3_ACCESS_KEY=<customer-secret-key-access-key>
DB_BACKUP_S3_SECRET_KEY=<customer-secret-key-secret>
```

Reference: <https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm>

## Required Production Settings

Add these values to `.env.production`:

```dotenv
DB_BACKUP_ENCRYPTION_KEY=<long-random-secret-stored-separately>
DB_BACKUP_PREFIX=database-backups
DB_BACKUP_RETENTION_DAYS=30
DB_BACKUP_HOUR_UTC=3
DB_BACKUP_RUN_ON_START=true
```

Keep the encryption key outside the bucket. Losing it makes encrypted backups unrecoverable; storing
it beside the backup defeats the purpose of client-side encryption.

## Deploy and Verify

```bash
docker compose --profile local --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --build --force-recreate

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  logs database-backup
```

For an immediate one-shot verification:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm -e DB_BACKUP_ONCE=true -e DB_BACKUP_RUN_ON_START=true database-backup
```

Confirm that both the `.sql.gz.enc` object and its `.sha256` object exist in the private bucket.

## Restore Drill

Always restore into a separate empty database first. Do not point this command at production.

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm \
  -e RESTORE_DATABASE_URL='postgresql://user:password@restore-host:5432/lifestack_restore' \
  -e CONFIRM_DATABASE_RESTORE=restore-lifestack-database \
  --entrypoint /usr/local/bin/database-restore \
  database-backup
```

Set `DB_BACKUP_OBJECT_KEY` to restore a specific object. If omitted, the latest encrypted/plain SQL
backup under `DB_BACKUP_PREFIX` is selected. A successful restore drill should include application
smoke checks and a record of the restored backup object and date.
