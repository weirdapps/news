"""Tests for orchestrator (main.py)."""

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from main import (
    _select_digest_articles,
    _setup_digest_pipeline,
    _source_health_note,
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


def test_source_health_note_names_a_silent_changelog_source(tmp_path):
    """A changelog source that goes quiet is a fetch warning nobody reads.

    Every other source type is already named in the digest footer; leaving
    changelog_sources out of the tuple is the gap that makes a dead vendor page
    look like a quiet publishing week.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    sources = {
        "rss_feeds": [{"name": "Live Feed"}],
        "changelog_sources": [{"name": "Claude System Prompts"}],
    }
    live = Article(
        url="https://example.com/live",
        title="Still publishing",
        source="Live Feed",
        content="body",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
        pipeline="stack",
    )
    live.compute_hash()
    assert insert_article(conn, live)

    note = _source_health_note(conn, sources, "stack")

    assert "Claude System Prompts" in note
    assert "Live Feed" not in note


def test_select_digest_articles_keeps_a_changelog_entry_in_a_production_shaped_pool():
    """Reaching build_stack_prompt is the whole point of the digest.

    The pool shape is the measured score histogram of a real stack run across
    20 sources. A future raise of max_articles_per_source, or a drop of
    max_digest_articles, is what would crowd a score-55 changelog entry out.
    """
    histogram = {65: 9, 62: 8, 60: 10, 37: 9, 35: 71, 32: 22, 30: 299, 25: 21, 20: 2}
    pool = []
    index = 0
    for score, count in histogram.items():
        for _ in range(count):
            pool.append(
                Article(
                    url=f"https://example.com/{index}",
                    title=f"Article {index}",
                    source=f"Source {index % 20}",
                    content="body",
                    categories=["ai"],
                    language="en",
                    relevance_score=score,
                )
            )
            index += 1
    assert len(pool) == 451

    entry = Article(
        url="https://platform.claude.com/docs/en/release-notes/system-prompts#claude-opus-5",
        title="Claude Opus 5 system prompt (July 24, 2026)",
        source="Claude System Prompts",
        content="The assistant is Claude, made by Anthropic.",
        categories=["releases"],
        language="en",
        relevance_score=55,
        changelog_digest="NEW MODEL ENTRY: ...",
        changelog_digest_source="deterministic",
    )
    pool.append(entry)

    selected = _select_digest_articles(
        pool, {"max_digest_articles": 150, "max_articles_per_source": 8}
    )

    assert entry in selected


_CHANGELOG_ENTRY_URL = (
    "https://platform.claude.com/docs/en/release-notes/system-prompts#claude-opus-5-july-24-2026"
)


def _stack_settings(tmp_path, transcripts_db):
    return {
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


def _empty_transcripts_db(tmp_path):
    path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    conn.close()
    return path


def _changelog_entry(digest="NEW MODEL ENTRY: the whole prompt is new."):
    return Article(
        url=_CHANGELOG_ENTRY_URL,
        title="Claude Opus 5 system prompt (July 24, 2026)",
        source="Claude System Prompts",
        content="The assistant is Claude, made by Anthropic.",
        categories=["releases"],
        language="en",
        published_at=datetime.now(UTC),
        pipeline="stack",
        changelog_digest=digest,
        changelog_digest_source="deterministic",
    )


def _upgrade_to_prose(articles, *args, **kwargs):
    for article in articles:
        article.changelog_digest = "Claude Opus 5 is now the selected model."
        article.changelog_digest_source = "llm"
    return len(articles), len(articles)


def test_stack_pipeline_upgrades_a_changelog_digest_before_storing(tmp_path):
    """The enrichment must sit after dedup and before the STORE loop.

    Run at the transcript enrichment site it would see the whole raw fetch,
    which at the measured per-call cost is ~2,900s against a 600s unit.
    """
    settings = _stack_settings(tmp_path, _empty_transcripts_db(tmp_path))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    stored: list[Article] = []

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"changelog_sources": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch(
            "main.fetch_all_sources",
            new_callable=AsyncMock,
            return_value=([_changelog_entry()], []),
        ),
        patch("main.insert_article", side_effect=lambda c, a: stored.append(a)),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        patch("news.stack_synth.synthesize_stack", return_value=("fallback", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
        patch("main.enrich_changelog_digests", side_effect=_upgrade_to_prose) as enrich,
    ):
        asyncio.run(run_stack_pipeline())

    assert enrich.call_count == 1
    assert [a.url for a in enrich.call_args[0][0]] == [_CHANGELOG_ENTRY_URL]
    assert len(stored) == 1
    assert stored[0].changelog_digest == "Claude Opus 5 is now the selected model."
    assert stored[0].changelog_digest_source == "llm"


def test_stack_pipeline_retries_a_changelog_entry_still_on_its_deterministic_delta(tmp_path):
    """A timed-out upgrade must be temporary, not permanent.

    The entry is a dedup drop on every later run, so unless the stale set puts
    it back in front of the enrichment it keeps the fallback forever.
    """
    settings = _stack_settings(tmp_path, _empty_transcripts_db(tmp_path))
    news_db = tmp_path / "news.db"
    conn = sqlite3.connect(news_db)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # Yesterday: the entry was stored, its LLM upgrade did not happen.
    stored_yesterday = _changelog_entry()
    stored_yesterday.compute_hash()
    assert insert_article(conn, stored_yesterday)

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"changelog_sources": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch(
            "main.fetch_all_sources",
            new_callable=AsyncMock,
            return_value=([_changelog_entry()], []),
        ),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        patch("news.stack_synth.synthesize_stack", return_value=("fallback", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
        patch("main.enrich_changelog_digests", side_effect=_upgrade_to_prose) as enrich,
    ):
        asyncio.run(run_stack_pipeline())

    assert [a.url for a in enrich.call_args[0][0]] == [_CHANGELOG_ENTRY_URL]
    check = sqlite3.connect(news_db)
    row = check.execute(
        "SELECT changelog_digest, changelog_digest_source FROM articles WHERE url = ?",
        (_CHANGELOG_ENTRY_URL,),
    ).fetchone()
    check.close()
    assert row == ("Claude Opus 5 is now the selected model.", "llm")


def test_stack_pipeline_makes_no_llm_call_when_nothing_carries_a_delta(tmp_path):
    """~92% of runs: no changelog entry survives dedup and the window."""
    settings = _stack_settings(tmp_path, _empty_transcripts_db(tmp_path))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    plain = Article(
        url="https://example.com/plain",
        title="An ordinary article",
        source="Live Feed",
        content="Nothing to do with a changelog at all.",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
    )

    with (
        patch("main.get_settings", return_value=settings),
        patch("main.get_sources", return_value={"rss_feeds": []}),
        patch("main.get_categories", return_value={}),
        patch("main.get_connection", return_value=conn),
        patch("main.init_db"),
        patch("main.get_last_digest", return_value=None),
        patch("main.fetch_all_sources", new_callable=AsyncMock, return_value=([plain], [])),
        patch("main.insert_article"),
        patch("main.get_articles_since", return_value=[]),
        patch("main.check_gcloud_auth", return_value=True),
        patch("news.stack_synth.synthesize_stack", return_value=("fallback", False)),
        patch("main.send_email", return_value=True),
        patch("main.insert_digest", return_value=1),
        patch("main.update_digest_sent"),
        patch("main.enrich_changelog_digests") as enrich,
    ):
        asyncio.run(run_stack_pipeline())

    enrich.assert_not_called()


# --- exit code contract -------------------------------------------------------
# A withheld run used to exit 0. systemd therefore fired OnSuccess=, hc-success@
# pinged Healthchecks green, and `systemctl --user list-units --failed` stayed
# empty while the inbox held "synthesis unavailable". The dead-man's switch cannot
# cover a failure the job reports as success.


def test_a_delivered_run_reports_delivered():
    import main as m

    assert m._delivered(synthesis_ok=True, sent_ok=True)
    assert m._delivered(synthesis_ok=True, sent_ok=None), "market is store-only, not undelivered"


def test_a_withheld_or_undelivered_run_reports_degraded():
    import main as m

    assert not m._delivered(synthesis_ok=False, sent_ok=True), "alert email is not a digest"
    assert not m._delivered(synthesis_ok=False, sent_ok=None)
    assert not m._delivered(synthesis_ok=True, sent_ok=False), "digest that never reached the inbox"


@pytest.mark.parametrize(
    "delivered,expected_exit",
    [(True, None), (False, 1)],
    ids=["delivered→0", "withheld→1"],
)
def test_main_turns_the_verdict_into_an_exit_code(monkeypatch, tmp_path, delivered, expected_exit):
    """The whole point: systemd has to be able to see a withheld run as a failure."""
    import main as m

    monkeypatch.setattr(sys, "argv", ["news", "--profile", "monitor", "--scheduled"])
    monkeypatch.setattr(m, "install_llm_deadline", lambda profile: None)
    monkeypatch.setattr(m, "acquire_lock", lambda path: True)
    monkeypatch.setattr(m, "release_lock", lambda path: None)

    async def _fake_pipeline(**kwargs):
        return delivered

    monkeypatch.setattr(m, "run_pipeline", _fake_pipeline)

    if expected_exit is None:
        m.main()  # must not raise SystemExit
    else:
        with pytest.raises(SystemExit) as exc:
            m.main()
        assert exc.value.code == expected_exit


def test_the_lock_is_released_even_when_the_run_is_withheld(monkeypatch):
    """sys.exit raises SystemExit, which must still pass through the finally block."""
    import main as m

    released = []
    monkeypatch.setattr(sys, "argv", ["news", "--profile", "monitor", "--scheduled"])
    monkeypatch.setattr(m, "install_llm_deadline", lambda profile: None)
    monkeypatch.setattr(m, "acquire_lock", lambda path: True)
    monkeypatch.setattr(m, "release_lock", lambda path: released.append(path))

    async def _withheld(**kwargs):
        return False

    monkeypatch.setattr(m, "run_pipeline", _withheld)

    with pytest.raises(SystemExit):
        m.main()
    assert len(released) == 1, "a withheld run must not leak the pipeline lock"


# --- verdict propagation through the dispatcher --------------------------------
# run_pipeline is the single hop between a pipeline's verdict and main()'s exit
# code. A branch that forgot its `return` would silently pin the exit code to 0 for
# that one profile, which is the exact failure the verdict exists to prevent and the
# hardest to notice: four profiles would still alert correctly.


@pytest.mark.parametrize(
    "profile,target,kwargs",
    [
        ("digest", "run_digest_pipeline", {}),
        ("monitor", "run_monitor_pipeline", {}),
        ("stack", "run_stack_pipeline", {}),
        ("market", "run_market_pipeline", {}),
        ("topic", "run_topic_pipeline", {"query": "anything"}),
    ],
)
@pytest.mark.parametrize("verdict", [True, False], ids=["delivered", "withheld"])
def test_run_pipeline_propagates_every_profiles_verdict(
    monkeypatch, profile, target, kwargs, verdict
):
    import main as m

    async def _fake(**_):
        return verdict

    monkeypatch.setattr(m, target, _fake)
    assert asyncio.run(m.run_pipeline(profile=profile, **kwargs)) is verdict


def test_an_unknown_profile_falls_through_to_the_digest():
    """The dispatcher's default arm still has to return, not drop the verdict."""
    import main as m

    async def _withheld(**_):
        return False

    with patch.object(m, "run_digest_pipeline", _withheld):
        assert asyncio.run(m.run_pipeline(profile="digest")) is False
