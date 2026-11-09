"""Tests for the Mac-side YouTube transcript harvester.

Everything is mocked: no network, no real transcript API, no claude CLI call.
"""

import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from news.transcripts import MAX_ATTEMPTS, init_transcript_db, upsert_transcript
from scripts.youtube_harvest import (
    ABSTRACT_MAX_CHARS,
    BLOCK_COOLDOWN,
    cooldown_until,
    distil,
    fetch_transcript,
    harvest,
    main,
    record_block,
    trim_to_sentence,
    videos_in_feed,
    youtube_channels,
)

# Transcripts long enough to pass the speech gate (MIN_SPEECH_WORDS), for tests
# whose subject is something else.
SPOKEN = " ".join(["spoken words"] * 40)
EXPENSIVE = " ".join(["the expensive transcript"] * 20)

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


def test_fetch_transcript_reports_an_ip_block_as_blocked():
    """IpBlocked is about this IP, not this video, so it gets its own status."""
    from youtube_transcript_api import IpBlocked

    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = IpBlocked("G55HSGpuh1M")

        text, status = fetch_transcript("G55HSGpuh1M")

    assert (text, status) == ("", "blocked")


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
    assert "--bare" in argv  # no plugins, hooks or MCP servers cold-started per video
    assert argv[argv.index("--tools") + 1] == ""  # fetched text must not drive tools
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
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(SPOKEN, "ok")) as fetch,
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
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(SPOKEN, "ok")),
        patch("scripts.youtube_harvest.distil", return_value=""),
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()

    assert row["status"] == "summary_failed"
    assert row["transcript"] == SPOKEN


def test_harvest_respects_the_run_limit(tmp_path):
    """Bounds spend per run; a backlog drains over successive runs."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(SPOKEN, "ok")) as fetch,
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
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(SPOKEN, "ok")),
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
        EXPENSIVE,
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
    assert distil_mock.call_args[0][0] == EXPENSIVE


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
        EXPENSIVE,
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
    assert row["transcript"] == EXPENSIVE


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


# --- IP block: stop asking, and say so ------------------------------------------
# From 2026-09-22 22:52 every hourly run went 0 for 25 on IpBlocked for a day and
# still exited 0, so the job graded green while the transcript feed sat empty.


def test_harvest_stops_at_the_first_ip_block_and_keeps_the_video_retryable(tmp_path):
    """Later fetches would be refused the same way, each burning a video's attempts."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", "blocked")) as fetch,
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    # SAMPLE_ATOM has two pending videos; the run stops after the first refusal.
    assert fetch.call_count == 1
    assert (stats["attempted"], stats["failed"], stats["blocked"]) == (1, 1, 1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()
    # The block says nothing about the video, so it stays retryable.
    assert row["status"] == "fetch_failed"


def _stats(**counts):
    stats = {
        "channels": 1,
        "attempted": 0,
        "ok": 0,
        "no_captions": 0,
        "failed": 0,
        "blocked": 0,
        "upcoming": 0,
        "refused": 0,
        "whisper": 0,
    }
    stats.update(counts)
    return stats


def _run_main(tmp_path, monkeypatch, stats=None, pushed=True):
    """Run main() with the harvest and the push mocked. Returns (rc, harvest, push)."""
    db_path = tmp_path / "transcripts.db"
    monkeypatch.setattr(sys, "argv", ["youtube_harvest.py", "--db", str(db_path)])
    with (
        patch("scripts.youtube_harvest._configure_logging"),
        patch("scripts.youtube_harvest.get_sources", return_value={}),
        patch("scripts.youtube_harvest.harvest", return_value=stats or _stats()) as harvest_mock,
        patch("scripts.youtube_harvest.push_to_vps", return_value=pushed) as push_mock,
    ):
        rc = main()
    return rc, harvest_mock, push_mock


@pytest.mark.parametrize(
    ("counts", "expected_rc"),
    [
        ({"attempted": 25, "failed": 25}, 1),  # every attempt failed
        ({"attempted": 25, "no_captions": 1, "failed": 24}, 1),
        ({}, 0),  # nothing new, or an empty channel list: a quiet hour, not a failure
        ({"attempted": 2, "no_captions": 2}, 0),  # a correct answer, not a failure
        ({"attempted": 25, "ok": 3, "failed": 22}, 0),  # partial progress
    ],
)
def test_main_fails_only_a_run_whose_attempts_all_failed(
    tmp_path, monkeypatch, counts, expected_rc
):
    rc, _, push = _run_main(tmp_path, monkeypatch, _stats(**counts))

    assert rc == expected_rc
    push.assert_called_once()  # the push happens either way


def test_main_fails_when_the_push_fails(tmp_path, monkeypatch):
    rc, _, _ = _run_main(tmp_path, monkeypatch, _stats(attempted=1, ok=1), pushed=False)

    assert rc == 1


def test_a_blocked_run_starts_a_cooldown_that_skips_the_fetch_but_still_pushes(
    tmp_path, monkeypatch
):
    """The VPS copy must stay the full store, so the push survives the cooldown."""
    blocked = _stats(attempted=1, failed=1, blocked=1)
    rc, _, _ = _run_main(tmp_path, monkeypatch, blocked)
    assert rc == 1
    assert cooldown_until(tmp_path / "transcripts.db") is not None

    rc, harvest_mock, push = _run_main(tmp_path, monkeypatch)

    harvest_mock.assert_not_called()
    push.assert_called_once()
    # Still a run that fetched nothing: a cooldown exiting 0 would grade green.
    assert rc == 1


def test_the_cooldown_lasts_its_window_and_then_lapses(tmp_path):
    db_path = tmp_path / "transcripts.db"
    start = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    record_block(db_path, True, now=start)

    assert cooldown_until(db_path, now=start + BLOCK_COOLDOWN - timedelta(minutes=1))
    assert cooldown_until(db_path, now=start + BLOCK_COOLDOWN + timedelta(minutes=1)) is None


def test_an_unblocked_run_clears_the_cooldown(tmp_path):
    db_path = tmp_path / "transcripts.db"
    record_block(db_path, True)

    record_block(db_path, False)

    assert cooldown_until(db_path) is None
    assert not (tmp_path / "transcripts.db.cooldown").exists()


def test_an_unreadable_cooldown_file_is_ignored(tmp_path, caplog):
    """Failing closed on a garbled file would stop the harvester for good."""
    db_path = tmp_path / "transcripts.db"
    (tmp_path / "transcripts.db.cooldown").write_text("not a timestamp\n")

    with caplog.at_level(logging.WARNING, logger="scripts.youtube_harvest"):
        assert cooldown_until(db_path) is None

    assert "unreadable cooldown file" in caplog.text


# --- Videos that have not aired yet, and distillations the model refuses ------


@pytest.mark.parametrize(
    "reason", ["Premieres in 21 hours", "This live event will begin in 3 hours"]
)
def test_fetch_transcript_reports_a_video_that_has_not_aired_as_upcoming(reason):
    """A premiere has no captions yet; that says nothing about whether it will."""
    from youtube_transcript_api._errors import VideoUnplayable

    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = VideoUnplayable("cZ_PqBpZygY", reason, [])

        text, status = fetch_transcript("cZ_PqBpZygY")

    assert (text, status) == ("", "upcoming")


def test_fetch_transcript_keeps_other_unplayable_videos_retryable():
    from youtube_transcript_api._errors import VideoUnplayable

    with patch("scripts.youtube_harvest.YouTubeTranscriptApi") as api:
        api.return_value.fetch.side_effect = VideoUnplayable("x", "Sign in to confirm", [])

        assert fetch_transcript("x") == ("", "fetch_failed")


def test_an_upcoming_video_spends_no_attempt_and_fails_no_run(tmp_path):
    """Eight hourly attempts ran out before a premiere 21 hours away ever aired."""
    db_path = tmp_path / "transcripts.db"

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", "upcoming")),
        patch("scripts.youtube_harvest.distil") as distil_mock,
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    distil_mock.assert_not_called()
    assert stats["failed"] == 0
    assert stats["upcoming"] == 2
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    conn.close()


def test_distil_raises_refused_when_the_model_declines_the_transcript():
    from scripts.youtube_harvest import DistilRefused

    refusal = (
        "API Error: Sonnet 5.5 can't help with this. Start a new session to continue.\n\n"
        "Learn more: https://www.anthropic.com/legal/aup\n\nDetails: `[cyber]`"
    )
    completed = Mock(returncode=1, stdout=refusal, stderr="")
    with (
        patch("scripts.youtube_harvest.subprocess.run", return_value=completed),
        pytest.raises(DistilRefused),
    ):
        distil("a DEF CON talk", "title")


def test_a_refused_distillation_is_settled_and_never_retried(tmp_path):
    """The same transcript draws the same refusal: retrying it spent 8 calls a video."""
    from scripts.youtube_harvest import DistilRefused

    db_path = tmp_path / "transcripts.db"
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(SPOKEN, "ok")),
        patch("scripts.youtube_harvest.distil", side_effect=DistilRefused("cyber")),
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    assert stats["failed"] == 0
    assert stats["refused"] == 2
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    from news.transcripts import pending_video_ids

    assert pending_video_ids(conn, ["G55HSGpuh1M", "abc12345678"]) == []
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()
    assert row["status"] == "refused"
    assert row["transcript"] == SPOKEN


@pytest.mark.parametrize(
    "counts", [{"attempted": 1, "upcoming": 1}, {"attempted": 2, "refused": 2}]
)
def test_main_passes_a_run_of_only_upcoming_or_refused_videos(tmp_path, monkeypatch, counts):
    rc, _, _ = _run_main(tmp_path, monkeypatch, _stats(**counts))

    assert rc == 0


def test_an_upcoming_video_does_not_take_a_slot_of_the_run_limit(tmp_path):
    """Premieres held 2 of the 3 hourly slots on 2026-11-09, so 1 real video an hour got through."""
    db_path = tmp_path / "transcripts.db"
    results = {"G55HSGpuh1M": ("", "upcoming"), "abc12345678": (SPOKEN, "ok")}

    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", side_effect=results.get),
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=1)

    assert stats["upcoming"] == 1
    assert stats["ok"] == 1
    assert stats["attempted"] == 1


# --- Audio fallback: local Whisper when captions give nothing -----------------


@pytest.fixture(autouse=True)
def _no_real_audio_fallback(request):
    """No test may download audio or load Whisper unless it asks for the real one."""
    if "real_audio_fallback" in request.keywords:
        yield
        return
    with patch("scripts.youtube_harvest.transcribe_audio", return_value=("", "unavailable")):
        yield


def _completed(stdout="", returncode=0):
    return Mock(returncode=returncode, stdout=stdout, stderr="")


@pytest.mark.real_audio_fallback
def test_transcribe_audio_refuses_a_video_longer_than_the_cap():
    from scripts.youtube_harvest import MAX_AUDIO_SECONDS, transcribe_audio

    probe = _completed(stdout=f"{MAX_AUDIO_SECONDS + 1}\n")
    with (
        patch("scripts.youtube_harvest.subprocess.run", return_value=probe) as run,
        patch.dict(sys.modules, {"mlx_whisper": Mock()}),
    ):
        assert transcribe_audio("ZZ19zhYnDKY") == ("", "too_long")

    assert run.call_count == 1  # probed, never downloaded


@pytest.mark.real_audio_fallback
def test_transcribe_audio_downloads_and_transcribes_with_whisper(tmp_path):
    from scripts.youtube_harvest import transcribe_audio

    def fake_run(cmd, **kwargs):
        if "--skip-download" in cmd:
            return _completed(stdout="998\n")
        out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "webm")
        Path(out).write_bytes(b"audio")
        return _completed()

    whisper = Mock()
    whisper.transcribe.return_value = {
        "segments": [{"text": " Hi everyone,"}, {"text": " I am Tanmay."}]
    }
    with (
        patch("scripts.youtube_harvest.subprocess.run", side_effect=fake_run),
        patch.dict(sys.modules, {"mlx_whisper": whisper}),
    ):
        assert transcribe_audio("BT1Mo-fD-g0") == ("Hi everyone, I am Tanmay.", "ok")

    model = whisper.transcribe.call_args.kwargs["path_or_hf_repo"]
    assert model.endswith("whisper-large-v3-turbo")  # never `base`: it mangles product names


@pytest.mark.real_audio_fallback
def test_transcribe_audio_is_unavailable_without_whisper():
    """The VPS has no mlx-whisper; the fallback must step aside, not fail videos."""
    from scripts.youtube_harvest import transcribe_audio

    with patch.dict(sys.modules, {"mlx_whisper": None}):
        assert transcribe_audio("BT1Mo-fD-g0") == ("", "unavailable")


@pytest.mark.real_audio_fallback
def test_transcribe_audio_fails_retryably_when_the_download_fails():
    from scripts.youtube_harvest import transcribe_audio

    def fake_run(cmd, **kwargs):
        if "--skip-download" in cmd:
            return _completed(stdout="998\n")
        return _completed(returncode=1)

    with (
        patch("scripts.youtube_harvest.subprocess.run", side_effect=fake_run),
        patch.dict(sys.modules, {"mlx_whisper": Mock()}),
    ):
        assert transcribe_audio("BT1Mo-fD-g0") == ("", "failed")


@pytest.mark.parametrize("caption_status", ["no_captions", "fetch_failed"])
def test_a_video_without_usable_captions_is_transcribed_from_its_audio(tmp_path, caption_status):
    db_path = tmp_path / "transcripts.db"
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", caption_status)),
        patch("scripts.youtube_harvest.transcribe_audio", return_value=(SPOKEN, "ok")),
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    assert stats["ok"] == 2
    assert stats["whisper"] == 2
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()
    assert (row["status"], row["transcript"], row["abstract"]) == (
        "ok",
        SPOKEN,
        "the abstract",
    )


@pytest.mark.parametrize(
    ("audio_status", "stored"),
    [("too_long", "too_long"), ("failed", "fetch_failed"), ("unavailable", "no_captions")],
)
def test_what_a_captionless_video_records_when_its_audio_gives_nothing(
    tmp_path, audio_status, stored
):
    db_path = tmp_path / "transcripts.db"
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", "no_captions")),
        patch("scripts.youtube_harvest.transcribe_audio", return_value=("", audio_status)),
        patch("scripts.youtube_harvest.distil") as distil_mock,
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    distil_mock.assert_not_called()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    assert row["status"] == stored
    if stored == "too_long":
        from news.transcripts import pending_video_ids

        assert "G55HSGpuh1M" not in pending_video_ids(conn, ["G55HSGpuh1M"])
    conn.close()


def test_a_blocked_ip_never_falls_back_to_audio(tmp_path):
    """A block means stop asking YouTube for anything, the audio included."""
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=("", "blocked")),
        patch("scripts.youtube_harvest.transcribe_audio") as audio,
    ):
        harvest(_FIRESHIP_SOURCES, tmp_path / "transcripts.db", limit=10)

    audio.assert_not_called()


def _seed(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    for video_id, status, published in rows:
        upsert_transcript(
            conn, video_id, "YouTube: GOTO", f"talk {video_id}", published, "", "", status
        )
    conn.close()


def test_backfill_transcribes_recent_failures_from_the_store_without_the_feed(tmp_path):
    """Older videos have left their channel's feed, so the store is the work list."""
    from scripts.youtube_harvest import backfill

    db_path = tmp_path / "transcripts.db"
    now = datetime.now(UTC)
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=45)).isoformat()
    _seed(
        db_path,
        [
            ("aaaaaaaaaaa", "no_captions", recent),
            ("bbbbbbbbbbb", "fetch_failed", recent),
            ("ccccccccccc", "no_captions", old),
            ("ddddddddddd", "refused", recent),
        ],
    )

    with (
        patch("scripts.youtube_harvest.fetch_transcript") as captions,
        patch("scripts.youtube_harvest.transcribe_audio", return_value=(SPOKEN, "ok")) as audio,
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        stats = backfill(db_path, days=30, limit=10)

    captions.assert_not_called()  # captions already failed for these
    assert sorted(c.args[0] for c in audio.call_args_list) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert stats["ok"] == 2
    conn = sqlite3.connect(db_path)
    statuses = dict(conn.execute("SELECT video_id, status FROM transcripts"))
    conn.close()
    assert statuses == {
        "aaaaaaaaaaa": "ok",
        "bbbbbbbbbbb": "ok",
        "ccccccccccc": "no_captions",
        "ddddddddddd": "refused",
    }


def test_backfill_respects_its_limit(tmp_path):
    from scripts.youtube_harvest import backfill

    db_path = tmp_path / "transcripts.db"
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _seed(db_path, [(f"vid{i:08d}", "no_captions", recent) for i in range(5)])

    with (
        patch("scripts.youtube_harvest.transcribe_audio", return_value=(SPOKEN, "ok")) as audio,
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        backfill(db_path, days=30, limit=2)

    assert audio.call_count == 2


@pytest.mark.real_audio_fallback
def test_transcribe_audio_reports_a_live_event_that_has_not_started_as_upcoming():
    from scripts.youtube_harvest import transcribe_audio

    probe = Mock(
        returncode=1,
        stdout="",
        stderr="ERROR: [youtube] y9JtCCZP2MQ: This live event will begin in 2 days.",
    )
    with (
        patch("scripts.youtube_harvest.subprocess.run", return_value=probe),
        patch.dict(sys.modules, {"mlx_whisper": Mock()}),
    ):
        assert transcribe_audio("y9JtCCZP2MQ") == ("", "upcoming")


def test_backfill_leaves_a_video_that_has_not_aired_untouched(tmp_path):
    from scripts.youtube_harvest import backfill

    db_path = tmp_path / "transcripts.db"
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _seed(db_path, [("y9JtCCZP2MQ", "fetch_failed", recent)])

    with patch("scripts.youtube_harvest.transcribe_audio", return_value=("", "upcoming")):
        stats = backfill(db_path, days=30, limit=5)

    assert (stats["attempted"], stats["failed"], stats["upcoming"]) == (0, 0, 1)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT attempts FROM transcripts").fetchone()[0] == 1  # the seed's own
    conn.close()


@pytest.mark.real_audio_fallback
def test_a_probe_that_times_out_is_a_retryable_failure():
    import subprocess

    from scripts.youtube_harvest import transcribe_audio

    with (
        patch(
            "scripts.youtube_harvest.subprocess.run",
            side_effect=subprocess.TimeoutExpired("yt-dlp", 60),
        ),
        patch.dict(sys.modules, {"mlx_whisper": Mock()}),
    ):
        assert transcribe_audio("BT1Mo-fD-g0") == ("", "failed")


@pytest.mark.real_audio_fallback
def test_a_download_that_times_out_is_a_retryable_failure():
    import subprocess

    from scripts.youtube_harvest import transcribe_audio

    def fake_run(cmd, **kwargs):
        if "--skip-download" in cmd:
            return _completed(stdout="998\n")
        raise subprocess.TimeoutExpired("yt-dlp", 900)

    with (
        patch("scripts.youtube_harvest.subprocess.run", side_effect=fake_run),
        patch.dict(sys.modules, {"mlx_whisper": Mock()}),
    ):
        assert transcribe_audio("BT1Mo-fD-g0") == ("", "failed")


@pytest.mark.real_audio_fallback
def test_a_whisper_crash_is_a_retryable_failure():
    from scripts.youtube_harvest import transcribe_audio

    def fake_run(cmd, **kwargs):
        if "--skip-download" in cmd:
            return _completed(stdout="998\n")
        Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "webm")).write_bytes(b"audio")
        return _completed()

    whisper = Mock()
    whisper.transcribe.side_effect = RuntimeError("Metal out of memory")
    with (
        patch("scripts.youtube_harvest.subprocess.run", side_effect=fake_run),
        patch.dict(sys.modules, {"mlx_whisper": whisper}),
    ):
        assert transcribe_audio("BT1Mo-fD-g0") == ("", "failed")


@pytest.mark.real_audio_fallback
def test_a_probe_without_a_duration_for_an_unknown_reason_is_a_retryable_failure():
    from scripts.youtube_harvest import transcribe_audio

    probe = Mock(returncode=1, stdout="", stderr="ERROR: [youtube] x: Sign in to confirm")
    with (
        patch("scripts.youtube_harvest.subprocess.run", return_value=probe),
        patch.dict(sys.modules, {"mlx_whisper": Mock()}),
    ):
        assert transcribe_audio("BT1Mo-fD-g0") == ("", "failed")


def test_backfill_can_be_limited_to_named_channels(tmp_path):
    """Recovering one channel's old gap must not reopen every channel's history."""
    from scripts.youtube_harvest import backfill

    db_path = tmp_path / "transcripts.db"
    recent = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_transcript_db(conn)
    upsert_transcript(
        conn, "anthropic01", "YouTube: Anthropic", "t", recent, "", "", "fetch_failed"
    )
    upsert_transcript(conn, "otherchan01", "YouTube: GOTO", "t", recent, "", "", "fetch_failed")
    conn.close()

    with (
        patch("scripts.youtube_harvest.transcribe_audio", return_value=(SPOKEN, "ok")) as audio,
        patch("scripts.youtube_harvest.distil", return_value="the abstract"),
    ):
        backfill(db_path, days=120, limit=10, channels=["YouTube: Anthropic"])

    assert [c.args[0] for c in audio.call_args_list] == ["anthropic01"]


# --- Transcripts too thin to summarise ----------------------------------------
# 36 stored "abstracts" (2026-11-09) were the model explaining that there was
# nothing to abstract: music-only launch videos, trailers, two-sentence clips.
# Stored as ok, they reached the digest as if they were summaries.


def test_speech_words_ignores_music_and_applause_cues():
    from scripts.youtube_harvest import speech_words

    assert speech_words("[music] [Music] ♪ >> Mhm. [Applause] Heat. Heat.") == 3
    assert speech_words("Today we ship Claude Code 3.0 with hooks.") == 8


def test_a_short_clip_with_real_speech_still_gets_summarised(tmp_path):
    """The gate is for silence, not brevity: a 35-word clip that says something passes."""
    db_path = tmp_path / "transcripts.db"
    clip = " ".join(["Black Hat Arsenal lets developers demo security tools live."] * 4)
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(clip, "ok")),
        patch("scripts.youtube_harvest.distil", return_value="Arsenal hosts live tool demos."),
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=1)

    assert stats["ok"] == 1


def test_a_transcript_with_almost_no_speech_is_never_sent_to_the_model(tmp_path):
    db_path = tmp_path / "transcripts.db"
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch(
            "scripts.youtube_harvest.fetch_transcript",
            return_value=("[music] [music] >> I love you. I love you. [music]", "ok"),
        ),
        patch("scripts.youtube_harvest.distil") as distil_mock,
    ):
        stats = harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    distil_mock.assert_not_called()
    assert stats["failed"] == 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    from news.transcripts import pending_video_ids

    assert pending_video_ids(conn, ["G55HSGpuh1M"]) == []  # settled, never retried
    conn.close()
    assert (row["status"], row["abstract"]) == ("too_thin", "")


@pytest.mark.parametrize(
    "reply",
    [
        "NO_CONTENT",
        "I can't write an abstract because the transcript contains only a single word.",
        "I cannot produce an abstract of this video.",
        "The transcript contains no usable content: only music cues.",
        "The transcript is too short to support a 600-800 character abstract.",
        "The transcript you've provided is too sparse to produce the abstract.",
        "The transcript you have provided contains no usable factual content.",
        "The transcript you've pasted is too short and contains no technical content.",
        "The transcript gives no benchmarks, versions, tool names or licences.",
        "This excerpt is a single sentence of welcome.",
    ],
)
def test_a_reply_that_declines_to_abstract_is_never_stored_as_a_summary(tmp_path, reply):
    db_path = tmp_path / "transcripts.db"
    words = " ".join(["speech"] * 80)
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch("scripts.youtube_harvest.fetch_transcript", return_value=(words, "ok")),
        patch("scripts.youtube_harvest.distil", return_value=reply),
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()
    assert (row["status"], row["abstract"]) == ("too_thin", "")
    assert row["transcript"] == words  # kept, as for every other settled outcome


def test_a_real_abstract_that_mentions_a_transcript_is_kept(tmp_path):
    """Only a reply that OPENS by declining counts; a summary may say 'transcript'."""
    db_path = tmp_path / "transcripts.db"
    abstract = "Anthropic released a transcript search tool for Claude Code sessions."
    with (
        patch("scripts.youtube_harvest.httpx.get", return_value=_atom_response()),
        patch(
            "scripts.youtube_harvest.fetch_transcript", return_value=(" ".join(["w"] * 80), "ok")
        ),
        patch("scripts.youtube_harvest.distil", return_value=abstract),
    ):
        harvest(_FIRESHIP_SOURCES, db_path, limit=10)

    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM transcripts WHERE video_id='G55HSGpuh1M'").fetchone()
    conn.close()
    assert status[0] == "ok"


def test_the_prompt_offers_the_model_a_way_to_say_there_is_nothing():
    completed = Mock(returncode=0, stdout="NO_CONTENT", stderr="")
    with patch("scripts.youtube_harvest.subprocess.run", return_value=completed) as run:
        distil("[music]", "Introducing Claude Sonnet 5.5")

    assert "NO_CONTENT" in run.call_args.kwargs["input"]
