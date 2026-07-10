# Spec 030: CLI Management Commands

**Status:** Implemented (`0c1eb2e`, merged 2026-06-12)
**Spec ID:** 030

## Problem Statement
Currently, background jobs (like weekly summaries, budget guardrails, and recurring transactions) can only be triggered on a scheduled interval (via embedded APScheduler) or through E2E test endpoints (`/v1/e2e/workflows/*`) which are gated to `local` and `test` environments.
Developers and administrators need a standard, CLI-based mechanism to trigger and retrigger these jobs manually inside the python environment (like Django management commands) without having to spin up a web server or expose API routes in production.

## Proposed Solution
We will introduce a CLI command runner `app/cli/run.py` that can be run using `python -m app.cli.run <job_name>`.
This tool will bootstrap the application context (settings, database session engine) and execute the requested job.

### CLI Interface
The CLI will support executing all background jobs currently defined in `app/application/jobs.py`:
- `budget_guardrails`
- `recurring_transactions`
- `fx_rate_ingestion`
- `export_cleanup`
- `session_cleanup`
- `import_preview_cleanup`
- `weekly_summary`

#### Command Usage
```bash
python -m app.cli.run <job_name> [--workspace-id <workspace_id>] [--week-start <YYYY-MM-DD>]
```

- `job_name`: Required name of the job to run.
- `--workspace-id`: Optional. If provided, limits the job execution to only the specified workspace ID (for jobs that support workspace isolation).
- `--week-start`: Optional. Specific to `weekly_summary`, allowing manual generation for a specific week (formatted as `YYYY-MM-DD`).

### Production Gating
To ensure E2E HTTP endpoints are absolutely not exposed in production:
1. The routing setup in [app/main.py](../../app/main.py) gates the `testing_router` to `settings.ENABLE_E2E_TEST_HOOKS` and `settings.ENV in {"local", "test"}`.
2. The config validator in [app/config.py](../../app/config.py) explicitly fails fast if `ENV == "production"` and `ENABLE_E2E_TEST_HOOKS == True`.
We will add automated tests to explicitly verify that `testing_router` routes are inaccessible and raise a `404` in staging/production setups, confirming the validation safety.

## Implementation Details

### CLI Runner (`app/cli/run.py`)
```python
import argparse
import asyncio
import sys
from datetime import date
from app.application.jobs import (
    budget_guardrails_job,
    recurring_transactions_job,
    fx_rate_ingestion_job,
    export_cleanup_job,
    session_cleanup_job,
    import_preview_cleanup_job,
    weekly_summary_job,
)

# Mapping of job names to their runner functions/coroutines
# ...
```

For jobs where we want to target a single workspace, we will extend the job runners in `app/application/jobs.py` or define a way to execute them for a specific workspace.

## Verification Plan

### Automated Tests
- Test that running the CLI entry point with invalid arguments prints usage and exits with error code 1.
- Test that executing a specific CLI job successfully calls the underlying workflow.
- Test production gating verification to guarantee E2E HTTP endpoints are 404'd in staging/production.

### Manual Verification
- Execute `python -m app.cli.run weekly_summary` locally and verify the summary is generated.
