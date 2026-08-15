"""Tests for source health reporting."""

from datetime import UTC, datetime, timedelta

from news.health import format_health_note, stale_sources
from news.models import Article
from news.storage import insert_article


def _store(db, source: str, age_hours: float, pipeline: str = "digest") -> None:
    when = datetime.now(UTC) - timedelta(hours=age_hours)
    article = Article(
        url=f"https://example.com/{source}/{age_hours}",
        title=f"Story from {source}",
        source=source,
        content="Some content here for the article body.",
        categories=["tech"],
        language="en",
        published_at=when,
        fetched_at=when,
        pipeline=pipeline,
    )
    article.compute_hash()
    insert_article(db, article)


def test_a_source_producing_today_is_not_stale(db):
    _store(db, "TechCrunch", age_hours=2)

    assert stale_sources(db, ["TechCrunch"], pipeline="digest", days=7) == []


def test_a_source_silent_beyond_the_threshold_is_reported(db):
    _store(db, "Dead Feed", age_hours=24 * 10)

    stale = stale_sources(db, ["Dead Feed"], pipeline="digest", days=7)

    assert len(stale) == 1
    assert stale[0][0] == "Dead Feed"
    assert stale[0][1] >= 9


def test_a_source_that_never_produced_anything_is_reported_with_no_age(db):
    """A newly added source that yields nothing on its first run is broken, not quiet."""
    stale = stale_sources(db, ["Never Worked"], pipeline="digest", days=7)

    assert stale == [("Never Worked", None)]


def test_a_quiet_weekend_does_not_flag_a_working_source(db):
    """The whole point: empty today must not read the same as dead.

    arXiv publishes on weekdays, so a Saturday run sees nothing from it. It
    produced three days ago, so it is healthy.
    """
    _store(db, "arXiv cs.CL", age_hours=24 * 3)

    assert stale_sources(db, ["arXiv cs.CL"], pipeline="digest", days=7) == []


def test_staleness_is_scoped_to_the_pipeline(db):
    """A source shared by two profiles can be healthy in one and dead in the other."""
    _store(db, "Shared Feed", age_hours=2, pipeline="stack")

    assert stale_sources(db, ["Shared Feed"], pipeline="stack", days=7) == []
    assert stale_sources(db, ["Shared Feed"], pipeline="digest", days=7) == [("Shared Feed", None)]


def test_results_are_ordered_worst_first(db):
    _store(db, "Recently Quiet", age_hours=24 * 8)
    _store(db, "Long Gone", age_hours=24 * 30)

    stale = stale_sources(db, ["Recently Quiet", "Long Gone", "Never Worked"], "digest", days=7)

    # Never-produced first, then longest silence.
    assert [s[0] for s in stale] == ["Never Worked", "Long Gone", "Recently Quiet"]


def test_format_health_note_is_empty_when_everything_is_healthy():
    assert format_health_note([]) == ""


def test_format_health_note_names_the_sources():
    note = format_health_note([("Never Worked", None), ("Long Gone", 30)])

    assert "2 sources" in note
    assert "Never Worked" in note
    assert "Long Gone" in note


def test_format_health_note_distinguishes_never_worked_from_went_quiet():
    """A source that never produced is a config error; one that stopped is a regression."""
    note = format_health_note([("Never Worked", None), ("Long Gone", 30)])

    assert "Never Worked (never)" in note
    assert "Long Gone (30d)" in note


def test_format_health_note_truncates_a_long_list_but_says_how_many():
    stale = [(f"Source {i}", 10) for i in range(12)]

    note = format_health_note(stale)

    assert "12 sources" in note
    assert "Source 0" in note
    # Must not dump all twelve into an email footer.
    assert "Source 11" not in note
