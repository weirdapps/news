#!/usr/bin/env python3
"""Weekly health check - verifies digest runs completed as expected."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def parse_run_log(log_path: Path, cutoff_date: datetime) -> tuple[int, int]:
    """Parse runs.log and count runs since cutoff.

    Args:
        log_path: Path to runs.log
        cutoff_date: Only count runs after this datetime

    Returns:
        Tuple of (total_runs, failed_runs)
    """
    if not log_path.exists():
        return 0, 0

    total_runs = 0
    failed_runs = 0

    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Parse timestamp (ISO format)
            try:
                timestamp_str = line.split("|")[0].strip()
                timestamp = datetime.fromisoformat(timestamp_str)

                # Skip if before cutoff
                if timestamp < cutoff_date:
                    continue

                total_runs += 1

                # Check for failures
                if "FAILED" in line:
                    failed_runs += 1

            except (ValueError, IndexError):
                # Skip malformed lines
                continue

    return total_runs, failed_runs


def check_health(log_path: Path, days: int = 7) -> dict:
    """Check health of digest runs over the last N days.

    Args:
        log_path: Path to runs.log
        days: Number of days to check (default 7)

    Returns:
        Dictionary with health check results
    """
    # Calculate cutoff date
    now = datetime.now(UTC)
    cutoff_date = now - timedelta(days=days)

    # Parse log
    total_runs, failed_runs = parse_run_log(log_path, cutoff_date)

    # Expected runs: 4 runs per day
    expected_runs = days * 4
    missed_runs = expected_runs - total_runs

    # Determine status
    if missed_runs > 3:
        status = "warning"
    elif missed_runs > 0:
        status = "ok"
    else:
        status = "healthy"

    return {
        "status": status,
        "total_runs": total_runs,
        "expected_runs": expected_runs,
        "missed_runs": missed_runs,
        "failed_runs": failed_runs,
        "days_checked": days,
        "cutoff_date": cutoff_date.isoformat(),
    }


def print_health_report(results: dict) -> None:
    """Print formatted health check report.

    Args:
        results: Health check results dictionary
    """
    status = results["status"]
    status_emoji = {
        "healthy": "✓",
        "ok": "⚠",
        "warning": "✗",
    }

    print("News Digest Health Check")
    print("=" * 50)
    print(f"Status: {status_emoji[status]} {status.upper()}")
    print(f"Period: Last {results['days_checked']} days")
    print(f"Since: {results['cutoff_date'][:10]}")
    print("")
    print(f"Expected runs: {results['expected_runs']}")
    print(f"Actual runs:   {results['total_runs']}")
    print(f"Missed runs:   {results['missed_runs']}")
    print(f"Failed runs:   {results['failed_runs']}")
    print("")

    if status == "warning":
        print("⚠ WARNING: More than 3 runs missed!")
        print("Check launchd logs for errors.")
    elif status == "ok":
        print("ℹ INFO: Some runs missed, but within acceptable range.")
    else:
        print("✓ All scheduled runs completed successfully.")


def main() -> None:
    """Main entry point."""
    # Determine project root and log path
    project_root = Path(__file__).resolve().parent.parent
    log_path = project_root / "data" / "runs.log"

    # Run health check
    results = check_health(log_path, days=7)

    # Print report
    print_health_report(results)

    # Exit with error if warning status
    if results["status"] == "warning":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
