"""Tests for the Mac-side YouTube transcript harvester.

Everything is mocked: no network, no real transcript API, no claude CLI call.
"""

import logging
import sqlite3
from unittest.mock import Mock, patch

from news.transcripts import MAX_ATTEMPTS, init_transcript_db, upsert_transcript
from scripts.youtube_harvest import (
    ABSTRACT_MAX_CHARS,
    distil,
    fetch_transcript,
    harvest,
    trim_to_sentence,
    videos_in_feed,
    youtube_channels,
)

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Fireship</title>
  <entry>
    <yt:videoId>G55HSGpuh1M</yt:videoId>
    <title>Meta's new model</title>
    <published>2026-08-11T18:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>abc12345678</yt:videoId>
    <title>Another video</title>
    <published>2026-08-10T18:00:00+00:00</published>
  </entry>
</feed>"""

_FIRESHIP_SOURCES = {
    "rss_feeds": [
        {
            "name": "YouTube: Fireship",
            "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA",
        }
    ]
}


# --- Channel discovery --------------------------------------------------------


def test_youtube_channels_extracts_ids_from_feed_urls():
    sources = {
        "rss_feeds": [
            {
                "name": "YouTube: Fireship",
                "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA",
            },
            {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
        ]
    }

    assert youtube_channels(sources) == [("YouTube: Fireship", "UCsBjURrPoezykLs9EqgamOA")]


def test_youtube_channels_ignores_a_youtube_url_without_a_channel_id():
    sources = {"rss_feeds": [{"name": "Broken", "url": "https://www.youtube.com/feeds/videos.xml"}]}

    assert youtube_channels(sources) == []


def test_videos_in_feed_extracts_id_title_and_date():
    videos = videos_in_feed(SAMPLE_ATOM)

    assert [v["video_id"] for v in videos] == ["G55HSGpuh1M", "abc12345678"]
    assert videos[0]["title"] == "Meta's new model"
    assert videos[0]["published"].startswith("2026-08-11")


# --- Transcript fetch ---------------------------------------------------------


def test_fetch_transcript_joins_snippet_text():
    snippets = [Mock(text="On Monday,"), Mock(text="Meta released a model.")]
    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.return_value = snippets

        text, status = fetch_transcript("G55HSGpuh1M")

    assert status == "ok"
    assert text == "On Monday, Meta released a model."


def test_fetch_transcript_marks_a_captionless_video_terminal():
    from youtube_transcript_api import TranscriptsDisabled

    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = TranscriptsDisabled("G55HSGpuh1M")

        text, status = fetch_transcript("G55HSGpuh1M")

    assert (text, status) == ("", "no_captions")


def test_fetch_transcript_marks_a_blocked_request_retryable():
    """An IP block is transient from our side; it must not be recorded as terminal."""
    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = RuntimeError("RequestBlocked")

        text, status = fetch_transcript("G55HSGpuh1M")

    assert (text, status) == ("", "fetch_failed")


# --- Distillation -------------------------------------------------------------


def test_distil_calls_the_claude_cli_and_returns_the_abstract(monkeypatch):
    """The tier alias must be resolved to an exact id, with its region pinned.

    A bare "sonnet" inherits whatever CLOUD_ML_REGION the parent carries. On the VPS
    that is `eu`, and sonnet in eu is an unprovisioned pairing that returns 429 --
    which the caller then logs as a generic CLI failure.
    """
    monkeypatch.setenv("VERTEX_MODEL_LIGHT", "claude-sonnet-4-6")
    monkeypatch.setenv("VERTEX_REGION_LIGHT", "europe-west1")
    completed = Mock(returncode=0, stdout="  Meta released Muse Glimmer under Apache 2.0.  ")
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed) as run:
        abstract = distil("the full spoken transcript", "Meta's new model")

    assert abstract == "Meta released Muse Glimmer under Apache 2.0."
    argv = run.call_args[0][0]
    assert argv[0] == "claude"
    assert "--model" in argv and "claude-sonnet-4-6" in argv
    assert "sonnet" not in argv  # the bare alias must not survive
    assert run.call_args[1]["env"]["CLOUD_ML_REGION"] == "europe-west1"


def test_distil_returns_empty_when_the_cli_fails():
    completed = Mock(returncode=1, stdout="", stderr="boom")
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed):
        assert distil("transcript", "title") == ""


def test_distil_prompt_tells_the_model_to_strip_sponsor_copy():
    """The whole point of the feature: the description sells, the transcript informs."""
    completed = Mock(returncode=0, stdout="abstract")
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed) as run:
        distil("transcript body", "A Title")

    prompt = run.call_args[1]["input"]
    assert "sponsor" in prompt.lower()
    assert "transcript body" in prompt


# --- Orchestration ------------------------------------------------------------


def _atom_response() -> Mock:
    resp = Mock(text=SAMPLE_ATOM, status_code=200)
    resp.raise_for_status = Mock()
    return resp


def test_harvest_skips_videos_already_settled_in_the_store(tmp_path):
    """Idempotence: the hourly run must not refetch or re-distil settled videos."""
    db_path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(conn, "G55HSGpuh1M", "Fireship", "Done", None, "t", "a", "ok")
    conn.close()

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch(
            "scripts.youtube_harvest.fetch_transcript", return_value=("spoken words", "ok")
        ) as fetch,
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    # SAMPLE_ATOM has two videos; one is already settled, so only one is fetched.
    assert fetch.call_count == 1
    assert stats["attempted"] == 1
    assert stats["ok"] == 1


def test_harvest_records_summary_failed_but_keeps_the_transcript(tmp_path):
    """Only the cheap half should be retried, so the expensive fetch is preserved."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("spoken words", "ok")),
        patch("scripts.youtube_harvest.distil", return_value=""),
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()

    assert row["status"] == "summary_failed"
    assert row["transcript"] == "spoken words"


def test_harvest_respects_the_run_limit(tmp_path):
    """Bounds spend per run; a backlog drains over successive runs."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch(
            "scripts.youtube_harvest.fetch_transcript", return_value=("spoken words", "ok")
        ) as fetch,
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=1)

    assert fetch.call_count == 1
    assert stats["attempted"] == 1


def test_harvest_survives_a_feed_that_fails_to_fetch(tmp_path):
    """One dead channel must not abort the whole run."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", side_effect=Exception("Connection refused")),
        patch("scripts.youtube_harvest.fetch_transcript") as fetch,
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    assert fetch.call_count == 0
    assert stats["channels"] == 1
    assert stats["attempted"] == 0


def test_harvest_records_a_captionless_video_so_it_is_never_retried(tmp_path):
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", "no_captions")),
        patch("scripts.youtube_harvest.distil") as distil_mock,
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    # No point distilling an empty transcript.
    assert distil_mock.call_count == 0
    assert stats["no_captions"] == 2


# --- Abstract length discipline -----------------------------------------------
# A live run produced a 1,119-char abstract against a 600-800 request. The
# synthesis snippet caps at 800, so an untrimmed abstract is cut mid-clause.


def test_trim_to_sentence_leaves_a_short_abstract_alone():
    text = "One fact. Two facts."
    assert trim_to_sentence(text, 800) == text


def test_trim_to_sentence_cuts_at_a_sentence_boundary_within_the_limit():
    text = "First sentence here. Second sentence here. Third runs past the limit."
    trimmed = trim_to_sentence(text, 45)

    assert trimmed == "First sentence here. Second sentence here."
    assert len(trimmed) <= 45


def test_trim_to_sentence_falls_back_to_a_hard_cut_when_there_is_no_boundary():
    text = "x" * 100
    assert len(trim_to_sentence(text, 40)) == 40


def test_harvest_stores_an_abstract_trimmed_to_the_synthesis_allowance(tmp_path):
    """An over-long abstract must be trimmed at store time, not cut mid-clause later."""
    db_path = tmp_path / "transcripts.db"
    long_abstract = ("A complete factual sentence about the model release. " * 40).strip()

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("words", "ok")),
        patch("scripts.youtube_harvest.distil", return_value=long_abstract),
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT abstract FROM transcripts").fetchone()
    conn.close()

    assert len(row["abstract"]) <= ABSTRACT_MAX_CHARS
    assert row["abstract"].endswith(".")


# --- Retry must not destroy work already done ---------------------------------


def test_harvest_reuses_a_stored_transcript_instead_of_refetching(tmp_path):
    """A summary_failed retry should cost one LLM call, not another fetch."""
    db_path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(
        conn,
        "G55HSGpuh1M",
        "Fireship",
        "Meta's new model",
        None,
        "the expensive transcript",
        "",
        "summary_failed",
    )
    conn.close()

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript") as fetch,
        patch("scripts.youtube_harvest.distil", return_value="the abstract") as distil_mock,
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=1)

    # The transcript was already in hand, so no refetch for that video.
    assert "G55HSGpuh1M" not in [c.args[0] for c in fetch.call_args_list]
    assert distil_mock.call_args[0][0] == "the expensive transcript"


def test_harvest_never_overwrites_a_stored_transcript_with_an_empty_one(tmp_path):
    """A failed refetch must not destroy a transcript we already paid to get."""
    db_path = tmp_path / "transcripts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(
        conn,
        "G55HSGpuh1M",
        "Fireship",
        "Meta's new model",
        None,
        "the expensive transcript",
        "",
        "summary_failed",
    )
    conn.close()

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", "fetch_failed")),
        patch("scripts.youtube_harvest.distil", return_value=""),
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=2)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT transcript FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()
    assert row["transcript"] == "the expensive transcript"


def test_harvest_treats_an_empty_caption_track_as_no_captions(tmp_path):
    """An 'ok' status with no text must not be sent to the LLM as an empty prompt."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("   ", "ok")),
        patch("scripts.youtube_harvest.distil") as distil_mock,
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=2)

    assert distil_mock.call_count == 0
    assert stats["no_captions"] == 2


def test_attempt_ceiling_survives_a_multi_hour_transient_outage():
    """Hourly runs plus a 3-attempt ceiling abandoned videos after 3 bad hours.

    Both observed failure modes are transient and multi-hour: YouTube IpBlocked
    (seen in a live run) and a Vertex 429. Losing a video permanently to either
    is the wrong trade, and retries are cheap now that transcripts are reused.
    """
    assert MAX_ATTEMPTS >= 8


def test_distil_logs_stdout_on_failure_because_that_is_where_vertex_errors_land(caplog):
    """A Vertex 429 arrives on stdout with stderr empty; logging stderr alone hides it."""
    completed = Mock(
        returncode=1,
        stdout="API Error: Request rejected (429) RESOURCE_EXHAUSTED quota exceeded",
        stderr="",
    )
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed):
        with caplog.at_level(logging.WARNING, logger="scripts.youtube_harvest"):
            assert distil("transcript", "title") == ""

    assert "429" in caplog.text
