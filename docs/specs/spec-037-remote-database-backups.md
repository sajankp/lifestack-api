# Spec 037: Remote Database Backups

**Status:** Implemented
**Approved:** 2026-06-20

## Scope

1. Run PostgreSQL backups outside the API process in a dedicated production sidecar.
2. Create a compressed plain-SQL dump daily.
3. Encrypt backups client-side before upload unless unencrypted operation is explicitly allowed.
4. Upload through an S3-compatible endpoint so the same mechanism works with Cloudflare R2 or
   Oracle Cloud Object Storage.
5. Retain backups for a configurable number of days and provide a guarded restore workflow.

## Backup Contract

- Required: `DATABASE_URL`, `DB_BACKUP_S3_ENDPOINT`, `DB_BACKUP_S3_BUCKET`,
  `DB_BACKUP_S3_ACCESS_KEY`, `DB_BACKUP_S3_SECRET_KEY`.
- Encryption: `DB_BACKUP_ENCRYPTION_KEY` is required unless
  `DB_BACKUP_ALLOW_UNENCRYPTED=true`.
- Output: `lifestack-YYYYMMDDTHHMMSSZ.sql.gz.enc` plus a SHA-256 sidecar.
- Schedule: daily at `DB_BACKUP_HOUR_UTC` with optional immediate startup execution.
- Retention: objects under `DB_BACKUP_PREFIX` older than `DB_BACKUP_RETENTION_DAYS` are removed.

## Restore Contract

Encrypted backups are decrypted, decompressed, and piped into `psql` against a separately selected
restore database. Production restore is always an explicit operator action.

Deployment and restore instructions live in
[`docs/PRODUCTION_DATABASE_BACKUPS.md`](../PRODUCTION_DATABASE_BACKUPS.md).
