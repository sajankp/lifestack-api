"""The audit script runs as a standalone process, not inside the app, so nothing
else imports every model. SQLAlchemy needs the whole metadata to resolve foreign
keys on INSERT (investing_cash_balances.source_import_id -> import_batches)."""

import subprocess
import sys


def test_audit_script_registers_all_tables_when_run_standalone():
    code = (
        "import scripts.audit_brokerage_cash\n"
        "from sqlmodel import SQLModel\n"
        "assert 'import_batches' in SQLModel.metadata.tables, 'import_batches missing'\n"
        "from app.investing.models import CashBalance\n"
        "from sqlalchemy.orm import configure_mappers\n"
        "configure_mappers()\n"
        "list(CashBalance.__table__.foreign_keys)[0].column\n"
        "for fk in CashBalance.__table__.foreign_keys:\n"
        "    fk.column\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-800:]
