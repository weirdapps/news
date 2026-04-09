"""Tests for email delivery module."""

import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from news.deliver import (
    build_subject,
    notify_macos,
    render_digest_html,
    save_fallback,
    send_email,
)

_ATHENS_TZ = ZoneInfo("Europe/Athens")


@pytest.fixture
def sample_synthesis():
    """Sample synthesis data for testing."""
    return {
        "executive_brief": [
            "Tech sector shows strong growth",
            "Banking regulations updated",
            "AI adoption accelerates",
            "Market volatility increases",
            "Climate concerns rise",
        ],
        "what_changed": ["New developments in AI", "Banking sector shifts"],
        "sections": [
            {
                "category": "tech",
                "display_name": "Technology",
                "synthesis": "Tech companies are seeing strong growth.\n\nInvestors remain optimistic.",
                "opposing_views": "Some analysts warn of bubble risks",
                "fact_check": "All statements fact-based",
                "sources": ["TechCrunch", "Wired"],
                "high_value": True,
            },
            {
                "category": "banking",
                "display_name": "Banking & Finance",
                "synthesis": "New regulations coming into effect.",
                "opposing_views": "None noted",
                "fact_check": "Pending official confirmation",
                "sources": ["FT", "WSJ"],
                "high_value": True,
            },
        ],
    }


def test_render_digest_html_produces_valid_html(sample_synthesis):
    """Verify HTML rendering with sample synthesis."""
    html = render_digest_html(
        synthesis=sample_synthesis,
        article_count=42,
        source_count=15,
        time_display="09:00",
        date_display="sat 5 apr",
        next_digest="tomorrow 09:00",
        subject="news digest — 09:00 sat 5 apr",
    )

    # Verify key strings present
    assert "NEWS DIGEST" in html
    assert "09:00" in html
    assert "sat 5 apr" in html
    assert "42 articles" in html
    assert "15 sources" in html
    assert "EXECUTIVE BRIEF" in html
    assert "Tech sector shows strong growth" in html
    assert "WHAT CHANGED" in html
    assert "New developments in AI" in html
    assert "Technology" in html
    assert "HIGH VALUE" in html
    assert "OPPOSING VIEWS" in html
    assert "FACT CHECK" in html
    assert "TechCrunch" in html

    # Verify no <p> tags
    assert "<p>" not in html and "<p " not in html

    # Verify Aptos font
    assert "Aptos" in html

    # Verify proper HTML structure
    assert "<!DOCTYPE html>" in html
    assert "xmlns" in html
    assert "</html>" in html


def test_render_digest_html_skips_empty_what_changed(sample_synthesis):
    """Verify What Changed section is hidden when empty."""
    # Remove what_changed
    synthesis_no_change = sample_synthesis.copy()
    synthesis_no_change["what_changed"] = []

    html = render_digest_html(
        synthesis=synthesis_no_change,
        article_count=42,
        source_count=15,
        time_display="09:00",
        date_display="sat 5 apr",
        subject="test",
    )

    # What Changed section should not appear
    assert "WHAT CHANGED" not in html

    # But executive brief should still be there
    assert "EXECUTIVE BRIEF" in html


def test_build_subject_scheduled():
    """Verify scheduled digest subject formatting."""
    dt = datetime(2026, 4, 5, 9, 0, tzinfo=_ATHENS_TZ)

    subject = build_subject(dt)

    assert subject == "News Digest | Sun 5 APR 09:00"


def test_build_subject_adhoc():
    """Verify ad-hoc digest subject includes ad-hoc marker."""
    dt = datetime(2026, 4, 5, 15, 42, tzinfo=_ATHENS_TZ)

    subject = build_subject(dt, is_adhoc=True)

    assert "News Digest" in subject
    assert "15:42" in subject
    assert "Sun 5 APR" in subject


def test_build_subject_partial_sources():
    """Verify partial sources suffix in subject."""
    dt = datetime(2026, 4, 5, 9, 0, tzinfo=_ATHENS_TZ)

    subject = build_subject(dt, partial_sources=True)

    assert "partial sources" in subject
    assert subject.endswith("partial sources")


def test_send_email_calls_gmail_script():
    """Verify send_email calls node script with correct args."""
    with patch("news.deliver.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = send_email(
            subject="test subject",
            html_body="<html>test</html>",
            recipient="test@example.com",
            gmail_script="/path/to/script.js",
        )

        assert result is True
        assert mock_run.call_count == 1

        # Verify command structure
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "node"
        assert "/path/to/script.js" in call_args
        assert "--to" in call_args
        assert "test@example.com" in call_args
        assert "--subject" in call_args
        assert "test subject" in call_args
        assert "--html" in call_args
        assert "<html>test</html>" in call_args


def test_save_fallback_writes_file(tmp_path):
    """Verify save_fallback writes HTML to file with timestamp."""
    html_content = "<html><body>Test digest</body></html>"

    filepath = save_fallback(html_content, output_dir=str(tmp_path))

    # Verify file exists
    assert Path(filepath).exists()

    # Verify content matches
    saved_content = Path(filepath).read_text(encoding="utf-8")
    assert saved_content == html_content

    # Verify filename format (YYYYMMDDHHMM_news_digest.html)
    filename = Path(filepath).name
    assert filename.endswith("_news_digest.html")
    assert len(filename.split("_")[0]) == 12  # YYYYMMDDHHMM


def test_notify_macos_handles_errors():
    """Verify notify_macos doesn't raise exceptions."""
    # Should not raise even if osascript fails
    with patch("news.deliver.subprocess.run", side_effect=Exception("fail")):
        notify_macos("Test", "Message")  # Should complete silently


def test_render_digest_html_with_fallback_text():
    """Verify fallback text is rendered when synthesis fails."""
    synthesis = {
        "executive_brief": ["Synthesis failed"],
        "what_changed": [],
        "sections": [],
        "fallback_text": "SYNTHESIS UNAVAILABLE\n\nCategorized headlines:\n\n## Tech\n- Article 1",
    }

    html = render_digest_html(
        synthesis=synthesis,
        article_count=10,
        source_count=5,
        time_display="09:00",
        date_display="sat 5 apr",
        subject="test",
    )

    assert "SYNTHESIS UNAVAILABLE" in html
    assert "Categorized headlines" in html
    assert "Article 1" in html


def test_build_subject_synthesis_failed():
    """Verify synthesis failed suffix in subject."""
    dt = datetime(2026, 4, 5, 9, 0, tzinfo=_ATHENS_TZ)

    subject = build_subject(dt, synthesis_failed=True)

    assert "headlines only" in subject
    assert subject.endswith("headlines only")
