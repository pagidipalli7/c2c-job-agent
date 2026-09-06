"""Run a backup now (the scheduler does this nightly at 03:15 Central)."""
import _bootstrap  # noqa: F401

from app.reporting.backup import run_backup

print(run_backup())
