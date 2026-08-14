"""Tests for the transcript store and article enrichment."""

import sqlite3
from pathlib import Path

import pytest

from news.models import Article
from news.transcripts import (
    MAX_ATTEMPTS,
    enrich_articles,
    extract_video_id,
    init_transcript_db,
    load_abstracts,
    pending_video_ids,
    upsert_transcript,
)


@pytest.fixture
def tconn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    yield conn
    conn.close()


# --- Store write path ---------------------------------------------------------


def test_upsert_stores_a_successful_transcript(tconn):
    upsert_transcript(
        tconn,
        "abc12345678",
        "Fireship",
        "A Video",
        "2026-08-11T00:00:00Z",
        transcript="the full spoken words",
        abstract="the distilled facts",
        status="ok",
    )

    row = tconn.execute("SELECT * FROM transcripts WHERE video_id='abc12345678'").fetchone()
    assert row["abstract"] == "the distilled facts"
    assert row["status"] == "ok"
    assert row["attempts"] == 1


def test_repeated_failures_increment_attempts(tconn):
    for _ in range(3):
        upsert_transcript(tconn, "vid00000001", "Fireship", "A Video", None, "", "", "fetch_failed")

    row = tconn.execute("SELECT * FROM transcripts WHERE video_id='vid00000001'").fetchone()
    assert row["attempts"] == 3


def test_pending_excludes_successful_videos(tconn):
    upsert_transcript(tconn, "done0000001", "Fireship", "Done", None, "t", "a", "ok")

    assert pending_video_ids(tconn, ["done0000001", "new00000001"]) == ["new00000001"]


def test_pending_excludes_videos_with_no_captions_permanently(tconn):
    """no_captions is terminal; that video will never gain captions."""
    upsert_transcript(tconn, "nocap000001", "Fireship", "No Caps", None, "", "", "no_captions")

    assert pending_video_ids(tconn, ["nocap000001"]) == []


def test_pending_retries_a_transient_failure_below_the_ceiling(tconn):
    upsert_transcript(tconn, "flaky000001", "Fireship", "Flaky", None, "", "", "fetch_failed")

    assert pending_video_ids(tconn, ["flaky000001"]) == ["flaky000001"]


def test_pending_gives_up_at_the_attempt_ceiling(tconn):
    for _ in range(MAX_ATTEMPTS):
        upsert_transcript(tconn, "dead0000001", "Fireship", "Dead", None, "", "", "fetch_failed")

    assert pending_video_ids(tconn, ["dead0000001"]) == []


def test_pending_returns_empty_for_no_candidates(tconn):
    assert pending_video_ids(tconn, []) == []


# --- Video id extraction ------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=G55HSGpuh1M", "G55HSGpuh1M"),
        ("https://youtube.com/watch?list=PL1&v=G55HSGpuh1M", "G55HSGpuh1M"),
        ("https://youtu.be/G55HSGpuh1M", "G55HSGpuh1M"),
        ("https://techcrunch.com/2026/08/14/story", None),
        ("", None),
    ],
)
def test_extract_video_id(url, expected):
    assert extract_video_id(url) == expected


# --- Store read path ----------------------------------------------------------


def _seed_store(tmp_path: Path) -> Path:
    db_path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(
        conn,
        "G55HSGpuh1M",
        "Fireship",
        "Muse Glimmer",
        None,
        "full words",
        "Meta released Muse Glimmer under Apache 2.0.",
        "ok",
    )
    upsert_transcript(conn, "nocap000001", "Fireship", "No Caps", None, "", "", "no_captions")
    conn.close()
    return db_path


def test_load_abstracts_returns_only_successful_records(tmp_path):
    db_path = _seed_store(tmp_path)

    got = load_abstracts(db_path, ["G55HSGpuh1M", "nocap000001"])

    assert got == {"G55HSGpuh1M": "Meta released Muse Glimmer under Apache 2.0."}


def test_load_abstracts_returns_empty_when_the_database_is_absent(tmp_path):
    """VPS first run, or a Mac that has never harvested. Must not raise."""
    assert load_abstracts(tmp_path / "nope.db", ["G55HSGpuh1M"]) == {}


def test_load_abstracts_returns_empty_for_no_ids(tmp_path):
    db_path = _seed_store(tmp_path)

    assert load_abstracts(db_path, []) == {}


# --- Article enrichment -------------------------------------------------------


def _article(url: str) -> Article:
    return Article(url=url, title="T", source="S", content="c", categories=[], language="en")


def test_enrich_articles_attaches_abstracts_to_video_items(tmp_path):
    db_path = _seed_store(tmp_path)
    articles = [_article("https://www.youtube.com/watch?v=G55HSGpuh1M")]

    enriched, total = enrich_articles(articles, db_path)

    assert (enriched, total) == (1, 1)
    assert articles[0].transcript_abstract == "Meta released Muse Glimmer under Apache 2.0."


def test_enrich_articles_leaves_non_youtube_articles_untouched(tmp_path):
    db_path = _seed_store(tmp_path)
    articles = [_article("https://techcrunch.com/story")]

    enriched, total = enrich_articles(articles, db_path)

    assert (enriched, total) == (0, 0)
    assert articles[0].transcript_abstract == ""


def test_enrich_articles_reports_partial_coverage(tmp_path):
    """A video the harvester has not reached yet counts to the total, not the enriched."""
    db_path = _seed_store(tmp_path)
    articles = [
        _article("https://www.youtube.com/watch?v=G55HSGpuh1M"),
        _article("https://www.youtube.com/watch?v=unseen00001"),
    ]

    enriched, total = enrich_articles(articles, db_path)

    assert (enriched, total) == (1, 2)


def test_enrich_articles_is_a_noop_when_the_store_is_missing(tmp_path):
    """Mac asleep: degrade to description-only, exactly as before the feature."""
    articles = [_article("https://www.youtube.com/watch?v=G55HSGpuh1M")]

    enriched, total = enrich_articles(articles, tmp_path / "nope.db")

    assert (enriched, total) == (0, 1)
    assert articles[0].transcript_abstract == ""
