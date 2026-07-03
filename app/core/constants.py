# ---------------------------------------------------------------------------
# Advisory Lock Keys
# ---------------------------------------------------------------------------
# Each background job acquires a Postgres advisory lock to ensure only one
# instance runs at a time across horizontally scaled workers.
#
# Keys MUST be globally unique integers.  Use pg_try_advisory_xact_lock so
# the lock is automatically released on transaction commit/rollback (no
# explicit unlock required and no risk of leaking back into the pool).
#
# Key registry:
#   1001 – budget_guardrails_job
#   1002 – recurring_transactions_job
#   1003 – weekly_summary_job
#   1004 – fx_rate_ingestion_job
#   1005 – export_cleanup_job
#   1006 – session_cleanup_job
#   1007 – import_preview_cleanup_job
#   1008 – bhavcopy_price_feed_job
#   1009 – dashboard_insights_job
#   1010 – push_delivery_job
#   1011 – todo_reminder_job
# ---------------------------------------------------------------------------

ADVISORY_LOCK_BUDGET_GUARDRAILS: int = 1001
ADVISORY_LOCK_RECURRING_TRANSACTIONS: int = 1002
ADVISORY_LOCK_WEEKLY_SUMMARY: int = 1003
ADVISORY_LOCK_FX_RATE_INGESTION: int = 1004
ADVISORY_LOCK_EXPORT_CLEANUP: int = 1005
ADVISORY_LOCK_SESSION_CLEANUP: int = 1006
ADVISORY_LOCK_IMPORT_PREVIEW_CLEANUP: int = 1007
ADVISORY_LOCK_BHAVCOPY_PRICE_FEED: int = 1008
ADVISORY_LOCK_DASHBOARD_INSIGHTS: int = 1009
ADVISORY_LOCK_PUSH_DELIVERY: int = 1010
ADVISORY_LOCK_TODO_REMINDER: int = 1011
