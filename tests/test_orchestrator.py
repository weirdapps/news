"""Tests for orchestrator (main.py)."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from main import (
    acquire_lock,
    get_next_digest_time,
    get_time_window,
    log_run,
    release_lock,
    run_digest_pipeline,
)


def test_acquire_and_release_lock(tmp_path):
    """Test lock acquisition and release."""
    lock_file = tmp_path / "test.lock"
    assert acquire_lock(str(lock_file)) is True
    assert lock_file.exists()
    assert acquire_lock(str(lock_file)) is False  # second acquire fails
    release_lock(str(lock_file))
    assert not lock_file.exists()


def test_get_time_window():
    """Test time window formatting."""
    now = datetime(2026, 4, 5, 13, 0, tzinfo=UTC)
    last_digest_at = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    window = get_time_window(now, last_digest_at, "Europe/Athens")
    assert "April" in window or "Apr" in window


def test_get_next_digest_time():
    """Test next digest time calculation."""
    schedule = ["09:00", "13:00", "17:00", "21:00"]
    assert get_next_digest_time("09:01", schedule) == "13:00"
    assert get_next_digest_time("21:01", schedule) == "09:00"  # wrap to tomorrow


def test_log_run(tmp_path):
    """Test run logging."""
    log_path = tmp_path / "runs.log"
    log_run(str(log_path), "scheduled", 42, 12, True, True, 8.3)
    content = log_path.read_text()
    assert "scheduled" in content
    assert "42" in content
    assert "synthesis OK" in content


def test_a_failed_digest_still_sends_exactly_one_alert_email(tmp_path):
    """When synthesis fails, exactly one alert email is sent — the owner's contract.

    The digest pipeline must send exactly one email per slot: the digest when
    synthesis works, or build_alert_html() carrying _SYNTH_FAIL_REASON when it
    does not. This test verifies the branch is wired to the policy's give-up
    (synthesize returning False) rather than to loop exhaustion that no longer
    exists after Task 3.
    """
    settings = {
        "pipeline": {
            "max_digest_articles": 100,
            "max_articles_per_source": 10,
            "min_article_length_words": 50,
            "max_article_age_hours": 48,
            "digest_window_hours": 48,
        },
        "email": {"recipient": "test@example.com"},
        "storage": {
            "db_path": str(tmp_path / "test.db"),
            "run_log_path": str(tmp_path / "runs.log"),
        },
        "schedule": {
            "timezone": "Europe/Athens",
            "runs": ["09:00", "13:00", "17:00", "21:00"],
        },
        "synthesis": {
            "max_retries": 2,
            "timeout": 60,
            "claude_command": "claude",
            "claude_args": [],
        },
        "scoring": {},
    }

    # Use a real in-memory connection so storage calls behave correctly.
    mem_conn = sqlite3.connect(":memory:")
    mem_conn.row_factory = sqlite3.Row

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"rss_feeds": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=mem_conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch("main.fetch_all_sources", new_callable=AsyncMock, return_value=([], [])),
        patch(
            "main.process_articles",
            return_value=([], {"output_count": 0, "duplicates": 0, "quality_dropped": 0}),
        ),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        # synthesize() returning (str, False) is the policy's give-up signal —
        # invoke_claude returned None, fallback text was substituted, ok=False.
        patch("main.synthesize", return_value=("fallback text", False)),
        patch("main.send_email", return_value=True) as mock_send,
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
    ):
        asyncio.run(run_digest_pipeline())

    assert mock_send.call_count == 1, (
        f"expected exactly one email per slot; got {mock_send.call_count}"
    )
    body = mock_send.call_args.kwargs.get("html_body", "")
    assert "AI synthesis could not be produced" in body, (
        "alert email must carry _SYNTH_FAIL_REASON; got: " + body[:200]
    )
