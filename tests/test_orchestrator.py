"""Tests for orchestrator (main.py)."""

from datetime import datetime, timezone

from main import (
    acquire_lock,
    get_next_digest_time,
    get_time_window,
    log_run,
    release_lock,
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
    now = datetime(2026, 4, 5, 13, 0, tzinfo=timezone.utc)
    last_digest_at = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
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
