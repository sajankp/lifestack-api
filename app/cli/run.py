import argparse
import asyncio
import sys
import traceback
from datetime import datetime

from app.application.jobs import (
    budget_guardrails_job,
    export_cleanup_job,
    fx_rate_ingestion_job,
    import_preview_cleanup_job,
    kpi_guardrails_job,
    load_reference_securities_job,
    medication_reminder_job,
    merge_company_identities_job,
    morning_briefing_job,
    net_worth_snapshot_job,
    recurring_transactions_job,
    session_cleanup_job,
    weekly_summary_job,
)

JOBS = {
    "budget_guardrails": budget_guardrails_job,
    "kpi_guardrails": kpi_guardrails_job,
    "recurring_transactions": recurring_transactions_job,
    "fx_rate_ingestion": fx_rate_ingestion_job,
    "export_cleanup": export_cleanup_job,
    "session_cleanup": session_cleanup_job,
    "import_preview_cleanup": import_preview_cleanup_job,
    "weekly_summary": weekly_summary_job,
    "net_worth_snapshot": net_worth_snapshot_job,
    "medication_reminder": medication_reminder_job,
    "morning_briefing": morning_briefing_job,
    "merge_company_identities": merge_company_identities_job,
    "load_reference_securities": load_reference_securities_job,
}

# Jobs that accept --workspace-id (optional for these; mandatory below for
# merge_company_identities specifically, per spec-083 §9).
WORKSPACE_SCOPED_JOBS = {
    "budget_guardrails",
    "recurring_transactions",
    "weekly_summary",
    "net_worth_snapshot",
    "medication_reminder",
    "morning_briefing",
    "merge_company_identities",
}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lifestack CLI Job Runner - manually trigger scheduler background tasks."
    )
    parser.add_argument(
        "job",
        choices=list(JOBS.keys()),
        help="The name of the background job to trigger.",
    )
    parser.add_argument(
        "--workspace-id",
        type=int,
        default=None,
        help="Optional. Limit execution of the job to this workspace ID.",
    )
    parser.add_argument(
        "--week-start",
        type=str,
        default=None,
        help="Optional. Specific to weekly_summary (Format: YYYY-MM-DD).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Optional. Specific to merge_company_identities: report what would "
        "change without writing anything.",
    )

    args = parser.parse_args()

    if args.dry_run and args.job != "merge_company_identities":
        print(
            "Error: --dry-run is only applicable for the 'merge_company_identities' job.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.job == "merge_company_identities" and args.workspace_id is None:
        print(
            "Error: --workspace-id is required for the 'merge_company_identities' job.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validations
    week_start_date = None
    if args.week_start is not None:
        if args.job != "weekly_summary":
            print(
                "Error: --week-start is only applicable for the 'weekly_summary' job.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            week_start_date = datetime.strptime(args.week_start, "%Y-%m-%d").date()
        except ValueError:
            print("Error: --week-start must be in YYYY-MM-DD format.", file=sys.stderr)
            sys.exit(1)
        if week_start_date.weekday() != 0:
            print("Error: --week-start must be a Monday.", file=sys.stderr)
            sys.exit(1)

    if args.workspace_id is not None and args.job not in WORKSPACE_SCOPED_JOBS:
        print(
            f"Error: --workspace-id is not supported for job '{args.job}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    job_func = JOBS[args.job]
    print(f"Starting job: {args.job}...")

    # Pass workspace_id/week_start if supported
    kwargs = {}
    if args.job in WORKSPACE_SCOPED_JOBS:
        kwargs["workspace_id"] = args.workspace_id
    if args.job == "weekly_summary":
        kwargs["week_start"] = week_start_date
    if args.job == "merge_company_identities":
        kwargs["dry_run"] = args.dry_run

    try:
        await job_func(**kwargs)
        print(f"Job '{args.job}' completed successfully.")
    except Exception as e:
        print(f"Error executing job '{args.job}': {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
