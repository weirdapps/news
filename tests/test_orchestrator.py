"""Tests for orchestrator (main.py)."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from main import (
    _setup_digest_pipeline,
    acquire_lock,
    get_next_digest_time,
    get_time_window,
    log_run,
    release_lock,
    run_digest_pipeline,
    run_stack_pipeline,
)
from news.models import Article
from news.storage import init_db, insert_article
from news.transcripts import init_transcript_db, upsert_transcript


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


# --- Source config assembled for non-RSS source types -------------------------


def _settings_for_setup(tmp_path):
    return {
        "pipeline": {},
        "email": {},
        "storage": {
            "db_path": str(tmp_path / "news.db"),
            "run_log_path": str(tmp_path / "run.log"),
        },
        "schedule": {},
    }


def test_setup_digest_pipeline_collects_age_overrides_from_every_source_type(tmp_path):
    sources = {
        "rss_feeds": [{"name": "TechCrunch", "tier": 1}],
        "api_sources": [{"name": "The Agent Daily", "tier": 1, "max_age_hours": 720}],
    }

    config = _setup_digest_pipeline(_settings_for_setup(tmp_path), sources)

    assert config["source_max_age"] == {"The Agent Daily": 720}


def test_setup_digest_pipeline_applies_declared_tiers_to_api_sources(tmp_path):
    """A tier declared on a non-RSS source must not silently score as tier 2."""
    sources = {
        "rss_feeds": [{"name": "TechCrunch", "tier": 1}],
        "api_sources": [{"name": "The Agent Daily", "tier": 1}],
    }

    config = _setup_digest_pipeline(_settings_for_setup(tmp_path), sources)

    assert config["source_tiers"]["The Agent Daily"] == 1


def test_setup_digest_pipeline_collects_age_overrides_from_changelog_sources(tmp_path):
    """System prompts move on model launches, so a 36h window never sees them."""
    sources = {
        "rss_feeds": [{"name": "TechCrunch", "tier": 1}],
        "changelog_sources": [{"name": "Claude System Prompts", "tier": 1, "max_age_hours": 720}],
    }

    config = _setup_digest_pipeline(_settings_for_setup(tmp_path), sources)

    assert config["source_max_age"] == {"Claude System Prompts": 720}


def test_setup_digest_pipeline_applies_declared_tiers_to_changelog_sources(tmp_path):
    sources = {
        "rss_feeds": [{"name": "TechCrunch", "tier": 1}],
        "changelog_sources": [{"name": "Claude System Prompts", "tier": 1}],
    }

    config = _setup_digest_pipeline(_settings_for_setup(tmp_path), sources)

    assert config["source_tiers"]["Claude System Prompts"] == 1


def test_digest_pipeline_keeps_an_old_article_from_a_source_with_an_age_override(tmp_path):
    """End to end: a 60h-old item from an override source must reach storage.

    The stack profile's window is 36h, so without the override plumbed from the
    source config all the way into filter_quality this article is dropped and
    the source contributes nothing — the exact failure the-agent-daily.org hit.
    """
    settings = {
        "pipeline": {
            "max_digest_articles": 100,
            "max_articles_per_source": 10,
            "min_article_length_words": 5,
            "max_article_age_hours": 36,
            "digest_window_hours": 36,
        },
        "email": {"recipient": "test@example.com"},
        "storage": {
            "db_path": str(tmp_path / "test.db"),
            "run_log_path": str(tmp_path / "runs.log"),
        },
        "schedule": {"timezone": "Europe/Athens", "runs": ["13:00"]},
        "synthesis": {"max_retries": 1, "timeout": 60, "claude_command": "claude"},
        "scoring": {},
    }
    sources = {
        "rss_feeds": [],
        "api_sources": [
            {
                "name": "The Agent Daily",
                "base_url": "https://the-agent-daily.org",
                "category": "industry",
                "tier": 1,
                "language": "en",
                "max_age_hours": 720,
            }
        ],
    }
    evergreen = Article(
        url="https://the-agent-daily.org/agentnews/deep-dives/harness-engineering",
        title="Harness Engineering",
        source="The Agent Daily",
        content="A deep dive into harness engineering for coding agents.",
        categories=["industry"],
        language="en",
        published_at=datetime.now(UTC) - timedelta(hours=60),
    )

    mem_conn = sqlite3.connect(":memory:")
    mem_conn.row_factory = sqlite3.Row
    init_db(mem_conn)
    stored: list[str] = []

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value=sources),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=mem_conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch(
            "main.fetch_all_sources",
            new_callable=AsyncMock,
            return_value=([evergreen], []),
        ),
        # Real process_articles runs; only the storage sink is observed.
        patch("main.insert_article", side_effect=lambda conn, a: stored.append(a.title)),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        patch("main.synthesize", return_value=("fallback text", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
    ):
        asyncio.run(run_digest_pipeline())

    assert stored == ["Harness Engineering"]


def test_stack_pipeline_attaches_transcript_abstracts_before_storing(tmp_path):
    """Enrichment must land before compute_hash/process_articles so scoring sees it."""
    transcripts_db = tmp_path / "transcripts.db"
    tconn = sqlite3.connect(transcripts_db)
    tconn.row_factory = sqlite3.Row
    init_transcript_db(tconn)
    upsert_transcript(
        tconn,
        "G55HSGpuh1M",
        "YouTube: Fireship",
        "Muse Glimmer",
        None,
        "full words",
        "Meta released Muse Glimmer under Apache 2.0.",
        "ok",
    )
    tconn.close()

    settings = {
        "pipeline": {
            "max_digest_articles": 100,
            "max_articles_per_source": 10,
            "min_article_length_words": 2,
            "max_article_age_hours": 36,
            "digest_window_hours": 36,
        },
        "email": {"recipient": "test@example.com"},
        "storage": {
            "db_path": str(tmp_path / "news.db"),
            "run_log_path": str(tmp_path / "runs.log"),
            "transcripts_db_path": str(transcripts_db),
        },
        "schedule": {"timezone": "Europe/Athens", "runs": ["13:00"]},
        "synthesis": {"max_retries": 1, "timeout": 60, "claude_command": "claude"},
        "scoring": {},
    }
    video = Article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        title="Muse Glimmer",
        source="YouTube: Fireship",
        content="Subscribe for more!",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
    )

    mem_conn = sqlite3.connect(":memory:")
    mem_conn.row_factory = sqlite3.Row
    init_db(mem_conn)
    stored: list[Article] = []

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"rss_feeds": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=mem_conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch("main.fetch_all_sources", new_callable=AsyncMock, return_value=([video], [])),
        patch("main.insert_article", side_effect=lambda conn, a: stored.append(a)),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        # NOT "main.synthesize_stack": main.py imports it locally inside the
        # function, so only the source-module patch takes effect.
        patch("news.stack_synth.synthesize_stack", return_value=("fallback", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
    ):
        asyncio.run(run_stack_pipeline())

    assert len(stored) == 1
    assert stored[0].transcript_abstract == "Meta released Muse Glimmer under Apache 2.0."


def test_stack_pipeline_backfills_an_abstract_onto_a_previously_stored_video(tmp_path):
    """The Mac-asleep case must degrade temporarily, not permanently.

    A video stored before the harvester reached it is dropped as a duplicate on
    every later run, because its hash is deliberately unaffected by the
    abstract. Without a backfill the enrichment is computed and thrown away.
    """
    transcripts_db = tmp_path / "transcripts.db"
    tconn = sqlite3.connect(transcripts_db)
    tconn.row_factory = sqlite3.Row
    init_transcript_db(tconn)
    upsert_transcript(
        tconn,
        "G55HSGpuh1M",
        "YouTube: Fireship",
        "Muse Glimmer",
        None,
        "full words",
        "Meta released Muse Glimmer under Apache 2.0.",
        "ok",
    )
    tconn.close()

    settings = {
        "pipeline": {
            "max_digest_articles": 100,
            "max_articles_per_source": 10,
            "min_article_length_words": 2,
            "max_article_age_hours": 36,
            "digest_window_hours": 36,
        },
        "email": {"recipient": "test@example.com"},
        "storage": {
            "db_path": str(tmp_path / "news.db"),
            "run_log_path": str(tmp_path / "runs.log"),
            "transcripts_db_path": str(transcripts_db),
        },
        "schedule": {"timezone": "Europe/Athens", "runs": ["13:00"]},
        "synthesis": {"max_retries": 1, "timeout": 60, "claude_command": "claude"},
        "scoring": {},
    }

    def _video() -> Article:
        return Article(
            url="https://www.youtube.com/watch?v=G55HSGpuh1M",
            title="Muse Glimmer",
            source="YouTube: Fireship",
            content="Subscribe for more!",
            categories=["ai"],
            language="en",
            published_at=datetime.now(UTC),
            pipeline="stack",
        )

    # File-backed, not :memory:, because the pipeline closes the connection and
    # the assertion has to read the row afterwards.
    news_db = tmp_path / "news.db"
    mem_conn = sqlite3.connect(news_db)
    mem_conn.row_factory = sqlite3.Row
    init_db(mem_conn)

    # Day 1: harvester had not run when the pipeline stored this video.
    day_one = _video()
    day_one.compute_hash()
    insert_article(mem_conn, day_one)
    assert mem_conn.execute("SELECT transcript_abstract FROM articles").fetchone()[0] is None

    # Day 2: same video still in the feed, abstract now available.
    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"rss_feeds": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=mem_conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch("main.fetch_all_sources", new_callable=AsyncMock, return_value=([_video()], [])),
        patch("main.check_gcloud_auth", return_value=True),
        patch("news.stack_synth.synthesize_stack", return_value=("fallback", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
    ):
        asyncio.run(run_stack_pipeline())

    check = sqlite3.connect(news_db)
    stored = check.execute("SELECT transcript_abstract FROM articles").fetchone()[0]
    check.close()
    assert stored == "Meta released Muse Glimmer under Apache 2.0."
