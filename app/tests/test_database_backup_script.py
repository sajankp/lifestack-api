import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_database_backup_creates_encrypted_archive_and_uploads_checksum(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "aws.log"

    _write_executable(
        bin_dir / "pg_dump", "#!/bin/sh\nprintf '%s\\n' 'CREATE TABLE demo(id int);'\n"
    )
    _write_executable(
        bin_dir / "openssl",
        """#!/bin/sh
in=''
out=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    -in) in="$2"; shift 2 ;;
    -out) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cp "$in" "$out"
""",
    )
    _write_executable(
        bin_dir / "aws",
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "{log_file}"
case "$*" in
  *list-objects-v2*) printf '%s\\n' '{{"Contents":[]}}' ;;
esac
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@db:5432/lifestack",
        "DB_BACKUP_S3_ENDPOINT": "https://example.invalid",
        "DB_BACKUP_S3_BUCKET": "backups",
        "DB_BACKUP_S3_ACCESS_KEY": "key",
        "DB_BACKUP_S3_SECRET_KEY": "secret",
        "DB_BACKUP_ENCRYPTION_KEY": "encryption-secret",
        "DB_BACKUP_RETENTION_DAYS": "30",
    }

    result = subprocess.run(
        ["bash", "scripts/database-backup.sh"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = log_file.read_text()
    assert ".sql.gz.enc" in calls
    assert ".sql.gz.enc.sha256" in calls
    assert "list-objects-v2" in calls


def test_database_backup_requires_encryption_by_default(tmp_path: Path):
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql://user:pass@db:5432/lifestack",
        "DB_BACKUP_S3_ENDPOINT": "https://example.invalid",
        "DB_BACKUP_S3_BUCKET": "backups",
        "DB_BACKUP_S3_ACCESS_KEY": "key",
        "DB_BACKUP_S3_SECRET_KEY": "secret",
    }

    result = subprocess.run(
        ["bash", "scripts/database-backup.sh"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "DB_BACKUP_ENCRYPTION_KEY is required" in result.stderr


def test_database_restore_requires_explicit_confirmation():
    env = {
        **os.environ,
        "RESTORE_DATABASE_URL": "postgresql://user:pass@db:5432/lifestack_restore",
        "DB_BACKUP_S3_ENDPOINT": "https://example.invalid",
        "DB_BACKUP_S3_BUCKET": "backups",
        "DB_BACKUP_S3_ACCESS_KEY": "key",
        "DB_BACKUP_S3_SECRET_KEY": "secret",
    }

    result = subprocess.run(
        ["bash", "scripts/database-restore.sh"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "CONFIRM_DATABASE_RESTORE=restore-lifestack-database" in result.stderr
