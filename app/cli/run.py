import argparse
import asyncio
import sys
import traceback
from datetime import datetime

from app.application.jobs import (
    budget_guardrails_job,
    constituent_ingestion_job,
    export_cleanup_job,
    fx_rate_ingestion_job,
    import_preview_cleanup_job,
    recurring_transactions_job,
    session_cleanup_job,
    weekly_summary_job,
)

JOBS = {
    "budget_guardrails": budget_guardrails_job,
    "recurring_transactions": recurring_transactions_job,
    "fx_rate_ingestion": fx_rate_ingestion_job,
    "constituent_ingestion": constituent_ingestion_job,
    "export_cleanup": export_cleanup_job,
    "session_cleanup": session_cleanup_job,
    "import_preview_cleanup": import_preview_cleanup_job,
    "weekly_summary": weekly_summary_job,
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

    args = parser.parse_args()

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

    if args.workspace_id is not None and args.job not in {
        "budget_guardrails",
        "recurring_transactions",
        "weekly_summary",
    }:
        print(
            f"Error: --workspace-id is not supported for job '{args.job}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    job_func = JOBS[args.job]
    print(f"Starting job: {args.job}...")

    # Pass workspace_id/week_start if supported
    kwargs = {}
    if args.job in {"budget_guardrails", "recurring_transactions", "weekly_summary"}:
        kwargs["workspace_id"] = args.workspace_id
    if args.job == "weekly_summary":
        kwargs["week_start"] = week_start_date

    try:
        await job_func(**kwargs)
        print(f"Job '{args.job}' completed successfully.")
    except Exception as e:
        print(f"Error executing job '{args.job}': {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
